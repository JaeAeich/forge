"""Incremental code index in Postgres: tree-sitter symbol chunks -> EmbeddingGemma vectors + MinHash signatures.

- Re-indexing a repo only touches files changed between the last indexed commit and HEAD.
- Semantic search is a pgvector HNSW nearest-neighbour query (cosine distance).
- Near-duplicate / contamination lookups use MinHash LSH: each signature is split into bands stored in an
  indexed table, so candidates come from an index lookup and only those are compared exactly.
"""

import time
from pathlib import Path
from typing import Any

import numpy as np

from . import code, db, llm, mine, queue

MAX_CHUNK_LINES = 120
EMBED_BATCH = 64


def chunk_file(source: str) -> list[tuple[str, str, int, int, str]]:
    """Functions and classes (big classes are covered by their methods); whole module if it has neither."""
    lines = source.splitlines()
    out = []
    for q, kind, s, e in code.symbols(source):
        if kind == "class" and e - s > MAX_CHUNK_LINES:
            continue
        e = min(e, s + 3 * MAX_CHUNK_LINES)
        out.append((q, kind, s, e, "\n".join(lines[s - 1 : e])))
    if not out and lines:
        out.append(("<module>", "module", 1, min(len(lines), MAX_CHUNK_LINES), "\n".join(lines[:MAX_CHUNK_LINES])))
    return out


def _store(c: db.Tx, repo: str, path: str, chunk: tuple[str, str, int, int, str], emb: np.ndarray) -> None:
    symbol, kind, start, end, text = chunk
    sig = code.minhash(text)
    row = c.one(
        "INSERT INTO chunks(repo,path,symbol,kind,start_line,end_line,text,emb,minhash) VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(repo, path, symbol, start_line) DO UPDATE SET kind=excluded.kind, end_line=excluded.end_line,"
        " text=excluded.text, emb=excluded.emb, minhash=excluded.minhash RETURNING id",
        (repo, path, symbol, kind, start, end, text, emb, sig.tobytes()),
    )
    c.execute("DELETE FROM chunk_bands WHERE chunk_id=?", (row["id"],))
    for band, key in enumerate(code.band_keys(sig)):
        c.execute(
            "INSERT INTO chunk_bands(chunk_id, band, key) VALUES(?,?,?) ON CONFLICT DO NOTHING", (row["id"], band, key)
        )


def _has_commit(path: Path, sha: str) -> bool:
    try:
        mine.git(path, "cat-file", "-e", f"{sha}^{{commit}}")
        return True
    except RuntimeError:
        return False


def index_repo(name: str, job: queue.Job | None = None) -> dict[str, Any]:
    """Index the repo as of its recorded `head` commit (read from git objects, not this worker's working tree,
    which may be at another commit or another worker's clone)."""
    repo = db.one("SELECT * FROM repos WHERE name=?", name)
    path = mine.repo_path(name)
    head, prev = repo["head"], repo["indexed_head"]
    if prev == head:
        return {"skipped": True}
    if not _has_commit(path, head):  # this worker's clone predates the mining run that recorded `head`
        mine.git(path, "fetch", "--quiet", "origin")
    if prev and not _has_commit(path, prev):  # history rewritten upstream: start over
        prev = None
    if prev:
        # --no-renames: a rename is a delete + add, so the old path's chunks get deleted too
        changed = mine.git(path, "diff", "--name-only", "--no-renames", prev, head, "--", "*.py").splitlines()
        for f in changed:
            db.ex("DELETE FROM chunks WHERE repo=? AND path=?", name, f)
    else:
        changed = [f for f in mine.git(path, "ls-tree", "-r", "--name-only", head).splitlines() if f.endswith(".py")]
        db.ex("DELETE FROM chunks WHERE repo=?", name)
    sources = {f: src for f in changed if (src := mine.file_at(path, head, f)) is not None}  # None: deleted
    files = [f for f, src in sources.items() if len(src) <= 300_000]
    rows = [(f, chunk) for f in files for chunk in chunk_file(sources[f])]
    t0 = time.time()
    for i in range(0, len(rows), EMBED_BATCH):
        if job:
            job.check()
        batch = rows[i : i + EMBED_BATCH]
        vectors = llm.embed([f"{f}::{chunk[0]}\n{chunk[4]}" for f, chunk in batch])
        with db.tx() as c:
            for (f, chunk), v in zip(batch, vectors, strict=True):
                _store(c, name, f, chunk, v)
    db.ex("UPDATE repos SET indexed_head=?, indexed_at=? WHERE name=?", head, time.time(), name)
    return {
        "files": len(files),
        "chunks": len(rows),
        "incremental": bool(prev),
        "chunks_per_s": round(len(rows) / max(1e-6, time.time() - t0), 1),
    }


def search(query: str, k: int = 10) -> dict[str, Any]:
    t0 = time.perf_counter()
    qv = llm.embed([query], kind="query")[0]
    t1 = time.perf_counter()
    results = db.q(
        "SELECT repo, path, symbol, kind, start_line, end_line, left(text, 1200) AS text,"
        " round((1 - (emb <=> ?))::numeric, 4)::float AS score FROM chunks ORDER BY emb <=> ? LIMIT ?",
        qv,
        qv,
        k,
    )
    return {
        "results": results,
        "embed_ms": round((t1 - t0) * 1000, 1),
        "scan_ms": round((time.perf_counter() - t1) * 1000, 2),
        "corpus": db.one("SELECT count(*) AS n FROM chunks")["n"],
    }


def _jaccard(sig: np.ndarray, blob: bytes) -> float:
    return code.jaccard_est(sig, np.frombuffer(blob, dtype=np.uint64))


def contamination(snippet: str, k: int = 5) -> dict[str, Any]:
    """Is this code (e.g. a task's reference solution) already in the corpus? LSH candidates + nearest vectors."""
    sig = code.minhash(snippet)
    vec = llm.embed([snippet])[0]
    keys = code.band_keys(sig)
    candidates = db.q(
        "SELECT chunk_id AS id FROM chunk_bands WHERE (band, key) IN (SELECT * FROM unnest(?::smallint[], ?::bigint[]))"
        " GROUP BY chunk_id ORDER BY count(*) DESC LIMIT 50",
        list(range(len(keys))),
        keys,
    )
    rows = db.q(
        "SELECT id, repo, path, symbol, left(text, 600) AS text, minhash, (1 - (emb <=> ?))::float AS cosine"
        " FROM chunks WHERE id = ANY(?) UNION"
        " (SELECT id, repo, path, symbol, left(text, 600), minhash, (1 - (emb <=> ?))::float FROM chunks"
        "  ORDER BY emb <=> ? LIMIT ?)",
        vec,
        [r["id"] for r in candidates],
        vec,
        vec,
        k,
    )
    for r in rows:  # replace the raw signature with its similarity before anything leaves this function
        r["jaccard"] = round(_jaccard(sig, r.pop("minhash")), 3)
        r["cosine"] = round(r["cosine"], 3)
    scored = sorted(rows, key=lambda m: -(m["jaccard"] + 0.25 * m["cosine"]))[:k]
    best = scored[0]["jaccard"] if scored else 0
    verdict = "near-duplicate" if best >= 0.7 else "similar" if best >= 0.4 else "novel"
    return {"verdict": verdict, "matches": scored}


def near_duplicates(threshold: float = 0.8, max_bucket: int = 50) -> dict[str, Any]:
    """Chunk pairs that share an LSH band (index join), verified by MinHash Jaccard. Ignores one-liners."""
    pairs = db.q(
        "WITH buckets AS (SELECT band, key FROM chunk_bands GROUP BY band, key HAVING count(*) BETWEEN 2 AND ?)"
        " SELECT DISTINCT a.chunk_id AS a, b.chunk_id AS b FROM buckets"
        " JOIN chunk_bands a USING (band, key) JOIN chunk_bands b USING (band, key) WHERE a.chunk_id < b.chunk_id",
        max_bucket,
    )
    ids = sorted({p["a"] for p in pairs} | {p["b"] for p in pairs})
    meta = {
        r["id"]: r
        for r in db.q("SELECT id, repo, path, symbol, start_line, end_line, minhash FROM chunks WHERE id = ANY(?)", ids)
    }

    def substantial(i: int) -> bool:
        return meta[i]["end_line"] - meta[i]["start_line"] >= 4

    def label(i: int) -> str:
        return f"{meta[i]['repo']}:{meta[i]['path']}::{meta[i]['symbol']}"

    found = []
    for p in pairs:
        a, b = meta[p["a"]], meta[p["b"]]
        if substantial(p["a"]) and substantial(p["b"]):
            j = code.jaccard_est(np.frombuffer(a["minhash"], np.uint64), np.frombuffer(b["minhash"], np.uint64))
            if j >= threshold:
                found.append((p["a"], p["b"], j))
    cross = [f for f in found if meta[f[0]]["repo"] != meta[f[1]]["repo"]]
    examples = sorted(cross or found, key=lambda f: -f[2])[:10]
    return {
        "pairs": len(found),
        "cross_repo": len(cross),
        "candidates_checked": len(pairs),
        "examples": [{"a": label(a), "b": label(b), "jaccard": round(j, 3)} for a, b, j in examples],
    }


def stats() -> list[dict[str, Any]]:
    return db.q(
        "SELECT c.repo, count(*) AS chunks, r.indexed_head, r.indexed_at FROM chunks c "
        "LEFT JOIN repos r ON r.name=c.repo GROUP BY c.repo, r.indexed_head, r.indexed_at ORDER BY c.repo"
    )
