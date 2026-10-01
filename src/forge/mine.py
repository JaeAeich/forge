"""Mine bug-fix commits that change both source and tests -> candidate tasks."""

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from . import code, config, db


def git(repo_path: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True, text=True, errors="replace")
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def git_bytes(repo_path: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True, check=True).stdout


def repo_path(name: str) -> Path:
    """Local clone of a configured repo, cloning it first if this worker has never seen it."""
    path = config.REPO_DIR / name
    if not (path / ".git").exists():
        sync_repo(name, config.REPOS[name])
    return path


def sync_repo(name: str, url: str) -> tuple[Path, str]:
    path = config.REPO_DIR / name
    if (path / ".git").exists():
        git(path, "fetch", "--quiet", "origin")
        git(path, "reset", "--quiet", "--hard", "FETCH_HEAD")
    else:
        config.REPO_DIR.mkdir(parents=True, exist_ok=True)
        # clone beside the target, then rename: concurrent workers never see a half-written clone
        tmp = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=config.REPO_DIR))
        try:
            subprocess.run(["git", "clone", "--quiet", "--no-tags", url, str(tmp)], check=True, capture_output=True)
        except BaseException:  # failed clone: don't leave a half-written temp dir behind
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        try:
            tmp.rename(path)
        except OSError:  # another worker won the race; use theirs
            shutil.rmtree(tmp, ignore_errors=True)
    head = git(path, "rev-parse", "HEAD").strip()
    db.ex(
        "INSERT INTO repos(name,url,head) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET head=excluded.head",
        name,
        url,
        head,
    )
    return path, head


def file_at(repo_path: Path, sha: str, path: str) -> str | None:
    r = subprocess.run(["git", "-C", str(repo_path), "show", f"{sha}:{path}"], capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


FIX_WORDS = ("fix", "bug", "issue", "error", "crash", "wrong", "incorrect", "handle", "regression", "broken", "support")


def candidates(repo_path: Path, limit: int) -> list[dict[str, Any]]:
    log = git(repo_path, "log", "--no-merges", f"-n{config.COMMITS_SCANNED}", "--format=%H%x00%P%x00%s%x00%b%x1e")
    out = []
    for rec in log.split("\x1e"):
        rec = rec.strip()
        if not rec:
            continue
        sha, parents, subject, body = [*rec.split("\x00"), "", "", "", ""][:4]
        if len(parents.split()) != 1:
            continue
        parent = parents.strip()
        changes = git(repo_path, "diff", "--numstat", "--no-renames", parent, sha)
        status = git(repo_path, "diff", "--name-status", "--no-renames", parent, sha)
        if any(line.split("\t")[0] not in ("M", "A") for line in status.splitlines() if line):
            continue
        src, tests, other, src_lines = [], [], [], 0
        for line in changes.splitlines():
            a, d, path = line.split("\t", 2)
            if not path.endswith(".py"):
                other.append(path)
                continue
            if code.is_test_path(path):
                tests.append(path)
            elif path.split("/")[-1] in code.CONFIG_FILES or path.startswith(("docs/", "doc/", "examples/")):
                other.append(path)
            else:
                src.append(path)
                src_lines += int(a if a != "-" else 0) + int(d if d != "-" else 0)
        doc_like = all(
            p.lower().endswith((".md", ".rst", ".txt")) or "changelog" in p.lower() or "changes" in p.lower()
            for p in other
        )
        if not (tests and 1 <= len(src) <= 3 and 2 <= src_lines <= 80 and doc_like):
            continue
        if not any(w in (subject + body).lower() for w in FIX_WORDS):
            continue
        out.append(
            {
                "sha": sha,
                "parent": parent,
                "subject": subject,
                "body": body.strip(),
                "src": src,
                "tests": tests,
                "src_lines": src_lines,
            }
        )
        if len(out) >= limit:
            break
    return out


def mine_repo(name: str, url: str) -> tuple[Path, str, list[str]]:
    path, head = sync_repo(name, url)
    existing = db.one("SELECT count(*) n FROM tasks WHERE repo=?", name)["n"]
    new = []
    if existing >= config.TASKS_PER_REPO:
        db.ex("UPDATE repos SET mined_at=? WHERE name=?", time.time(), name)
        return path, head, new
    for c in candidates(path, config.TASKS_PER_REPO + 50):
        tid = f"{name}@{c['sha'][:10]}"
        if db.q1("SELECT 1 FROM tasks WHERE id=?", tid):
            continue
        gold = git(path, "diff", "--no-renames", c["parent"], c["sha"], "--", *c["src"])
        test_patch = git(path, "diff", "--no-renames", c["parent"], c["sha"], "--", *c["tests"])
        parsed = code.parse_patch(gold)
        syms = []
        for f in c["src"]:
            src_after = file_at(path, c["sha"], f) or ""
            syms += [f"{f}::{s}" for s in code.enclosing_symbols(src_after, parsed.get(f, {}).get("added", set()))]
        db.insert(
            "tasks",
            id=tid,
            repo=name,
            sha=c["sha"],
            parent=c["parent"],
            title=c["subject"],
            commit_msg=(c["subject"] + "\n\n" + c["body"]).strip(),
            gold_patch=gold,
            test_patch=test_patch,
            src_files=json.dumps(c["src"]),
            test_files=json.dumps(c["tests"]),
            symbols=json.dumps(syms),
            lines_changed=c["src_lines"],
            status="candidate",
            created=time.time(),
        )
        new.append(tid)
        if existing + len(new) >= config.TASKS_PER_REPO:
            break
    db.ex("UPDATE repos SET mined_at=? WHERE name=?", time.time(), name)
    return path, head, new
