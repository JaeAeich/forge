"""Code index on pgvector + MinHash LSH, with a deterministic stand-in for the embedding model."""

import hashlib

import numpy as np
import pytest

from forge import index, llm

SNIPPETS = {
    "lru": "def evict(cache):\n    key = next(iter(cache.order))\n    cache.order.pop(key)\n    return cache.data.pop(key)\n",
    "add": "def add(a, b):\n    total = a + b\n    log(total)\n    return total\n",
    "io": "def read(path):\n    with open(path) as f:\n        data = f.read()\n    return data.strip()\n",
}


def fake_embed(texts, kind="doc"):
    out = []
    for t in texts:
        seed = int.from_bytes(hashlib.sha1(t.strip().encode()).digest()[:4], "little")
        v = np.random.default_rng(seed).normal(size=768).astype(np.float32)
        out.append(v / np.linalg.norm(v))
    return np.array(out)


@pytest.fixture()
def corpus(pg, monkeypatch):
    monkeypatch.setattr(llm, "embed", fake_embed)
    with pg.tx() as c:
        for repo in ("a", "b"):  # identical code in two repos -> cross-repo near-duplicates
            for name, text in SNIPPETS.items():
                chunk = (name, "function", 1, 5, text)
                index._store(c, repo, f"{name}.py", chunk, fake_embed([text])[0])
    return pg


def test_vector_search_returns_nearest_first(corpus):
    res = index.search(SNIPPETS["io"])
    assert res["corpus"] == 6
    assert res["results"][0]["symbol"] == "io"
    assert res["results"][0]["score"] == pytest.approx(1.0, abs=1e-3)


def test_contamination_finds_exact_copies(corpus):
    res = index.contamination(SNIPPETS["lru"])
    assert res["verdict"] == "near-duplicate"
    assert res["matches"][0]["symbol"] == "lru" and res["matches"][0]["jaccard"] == 1.0
    assert all("minhash" not in m for m in res["matches"])  # raw signatures must not leak into responses


def test_near_duplicates_across_repos(corpus):
    res = index.near_duplicates()
    assert res["pairs"] == 3 and res["cross_repo"] == 3
