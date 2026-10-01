"""Task validation: fail-before/pass-after, flakiness reruns, issue synthesis, leak + solvability checks."""

import hashlib
import json
import re
import time
from typing import Any

from . import code, db, llm, mine, queue, sandbox
from .sandbox import Sandbox

RESULT = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL) (\S+)", re.M)
BEFORE_RUNS, AFTER_RUNS = 2, 3


def test_cmd(files: list[str]) -> str:
    return "PYTHONPATH=/repo/src:/repo python -m pytest -rA -q --tb=line -p no:cacheprovider -o addopts='' " + " ".join(
        files
    )


def parse_results(out: str) -> dict:
    res = {}
    for kind, node in RESULT.findall(out):
        res[node] = kind
    return res


def run_tests(sb: Sandbox, files: list[str], timeout: int = 180) -> tuple[int, dict, str]:
    rc, out = sb.exec(test_cmd(files), timeout=timeout)
    return rc, parse_results(out), out


def task_tag(task_id: str) -> str:
    # keyed on the base image too: rebuilding the sandbox image must invalidate every task snapshot
    key = hashlib.sha1(f"{task_id}|{sandbox.base_id()}".encode()).hexdigest()[:8]
    return f"{re.sub(r'[^a-z0-9_.-]', '-', task_id.lower())[:60]}-{key}"


def ensure_task_image(task: db.Row) -> str:
    """Snapshot of a sandbox holding the repo at the parent of the fix commit. Returns its ref."""
    tag = task_tag(task["id"])
    if sandbox.exists(ref := sandbox.ref("forge-task", tag)):
        return ref  # made before, here or by another worker
    tar = mine.git_bytes(mine.repo_path(task["repo"]), "archive", "--format=tar", task["parent"])
    with Sandbox.create() as sb:
        sb.put_tar(tar, "/repo")
        rc, out = sb.exec("git init -q && git add -A && git commit -qm base && echo ok")
        if rc:
            raise RuntimeError(f"task image setup failed: {out[-500:]}")
        ref, _ = sb.snapshot("forge-task", tag)
    return ref


def _files_at_fix(task: db.Row, column: str) -> dict[str, str]:
    """Contents of the task's files at the fix commit. Mining only keeps added/modified files, so all exist."""
    repo = mine.repo_path(task["repo"])
    out = {}
    for f in json.loads(task[column]):
        content = mine.file_at(repo, task["sha"], f)
        if content is None:
            raise RuntimeError(f"{task['id']}: {f} missing at {task['sha']}")
        out[f] = content
    return out


def hidden_tests(task: db.Row) -> dict[str, str]:
    return _files_at_fix(task, "test_files")


def gold_files(task: db.Row) -> dict[str, str]:
    return _files_at_fix(task, "src_files")


def reject(task_id: str, reason: str, **extra: Any) -> dict[str, Any]:
    db.update("tasks", "id", task_id, status="rejected", reject_reason=reason, validated_at=time.time(), **extra)
    return {"status": "rejected", "reason": reason}


def validate(task_id: str, job: queue.Job | None = None) -> dict[str, Any]:
    task = db.one("SELECT * FROM tasks WHERE id=?", task_id)
    if task["status"] != "candidate":
        return {"status": task["status"]}
    image = ensure_task_image(task)
    db.update("tasks", "id", task_id, image=image)
    tests = hidden_tests(task)
    files = list(tests)
    with Sandbox.create(image) as sb:
        sb.put({f"/repo/{f}": c for f, c in tests.items()})
        before = [run_tests(sb, files)[1] for _ in range(BEFORE_RUNS)]
        sb.put({f"/repo/{f}": c for f, c in gold_files(task).items()})
        after_runs = [run_tests(sb, files) for _ in range(AFTER_RUNS)]
    after = [r[1] for r in after_runs]
    if job:
        job.check()
    if not after[0]:
        return reject(
            task_id,
            "no test results after fix (collection/env failure)",
            judge=json.dumps({"log": after_runs[0][2][-2000:]}),
        )
    if any(b != before[0] for b in before) or any(a != after[0] for a in after):
        return reject(task_id, "flaky: outcomes differ across reruns")
    passed_before = {t for t, o in before[0].items() if o == "PASSED"}
    passed_after = {t for t, o in after[0].items() if o == "PASSED"}
    f2p = sorted(passed_after - passed_before)
    p2p = sorted(passed_after & passed_before)
    if not f2p:
        return reject(task_id, "no fail-to-pass tests", f2p="[]", p2p=json.dumps(p2p))
    db.update("tasks", "id", task_id, f2p=json.dumps(f2p), p2p=json.dumps(p2p))

    issue = write_issue(task, f2p)
    leak = leak_score(issue, task["gold_patch"])
    judge = judge_task(task, issue, f2p)
    judge["leak"] = leak
    db.update("tasks", "id", task_id, issue=issue, judge=json.dumps(judge))
    if leak["verbatim_lines"] or leak["shingle_overlap"] > 0.25:
        return reject(task_id, "issue leaks the solution")
    if int(judge.get("solvable", 0) or 0) < 3:
        return reject(task_id, f"judge: underspecified ({judge.get('reason', '')[:200]})")
    db.update("tasks", "id", task_id, status="valid", validated_at=time.time())
    return {"status": "valid", "f2p": len(f2p), "p2p": len(p2p)}


def write_issue(task: db.Row, f2p: list[str]) -> str:
    prompt = f"""You are turning a git commit into a GitHub issue for a coding benchmark.
Write the issue a USER would have filed BEFORE the fix existed: describe the observed wrong behavior and the
expected behavior, ideally with a short reproduction snippet calling the public API. Do NOT describe the fix,
do NOT include code from the fix, do NOT mention test names or file paths of tests.

Commit message:
{task["commit_msg"][:1500]}

Changes to tests (shows the expected behavior):
{task["test_patch"][:3500]}

Return JSON: {{"title": "...", "body": "..."}}"""
    r = llm.json_call(prompt)
    title = r.get("title") or task["title"]
    body = r.get("body") or task["commit_msg"]
    return f"# {title}\n\n{body}".strip()


def leak_score(issue: str, gold_patch: str) -> dict[str, Any]:
    added = []
    for f in code.parse_patch(gold_patch).values():
        added += f["added_text"]
    verbatim = [line.strip() for line in added if len(line.strip()) >= 30 and line.strip() in issue]
    gs = code.shingles("\n".join(added), k=8)
    ish = code.shingles(issue, k=8)
    overlap = len(gs & ish) / max(1, len(gs))
    return {"verbatim_lines": verbatim[:5], "shingle_overlap": round(overlap, 3)}


def judge_task(task: db.Row, issue: str, f2p: list[str]) -> dict[str, Any]:
    prompt = f"""Grade this benchmark task for FAIRNESS. An engineer sees only the ISSUE and the repository.
Hidden tests will check the fix. Is the issue specific enough that a competent engineer would write a fix that
passes the hidden tests, without guessing arbitrary names/formats/messages only visible in the tests?

ISSUE:
{issue[:3000]}

HIDDEN TEST CHANGES:
{task["test_patch"][:3000]}

Return JSON: {{"solvable": <1-5, 5 = fully specified>, "reason": "<one sentence>"}}"""
    r = llm.json_call(prompt)
    try:
        r["solvable"] = int(r.get("solvable", 0))
    except Exception:
        r["solvable"] = 0
    return r
