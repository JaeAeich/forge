"""Ephemeral multi-node environments with consistent group snapshots.

A stack = app (python) + postgres + redis, one multi-node sandbox whose nodes reach each other by name.
Snapshot protocol:
  1. quiesce hooks: redis SAVE, postgres CHECKPOINT (flush state to disk)
  2. snapshot every node. Docker additionally pauses every node while it commits them, so no write is in
     flight anywhere; Kubernetes cuts one volume snapshot per node right after the quiesce.
Restore/fork = a new sandbox from the snapshot ref, so any number of independent copies run side by side.
"""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import config, db, sandbox
from .sandbox import Node, Sandbox


def nodes() -> list[Node]:
    return [
        Node("app", config.ENV_APP_IMAGE),
        Node(
            "db", "postgres:16-alpine", env={"POSTGRES_PASSWORD": "forge", "PGDATA": "/pgdata/data"}, volume="/pgdata"
        ),
        Node(
            "cache",
            "redis:7-alpine",
            command=("sh", "-c", "mkdir -p /rdata && exec redis-server --dir /rdata --save ''"),
            volume="/rdata",
        ),
    ]


def ensure_images() -> None:
    sandbox.ensure_image(config.ENV_APP_IMAGE, "env-app")


def create(from_snapshot: int | None = None) -> dict[str, Any]:
    ensure_images()
    env_id = db.insert("envs", name="stack", status="starting", from_snapshot=from_snapshot, created=time.time())
    ref = db.one("SELECT ref FROM env_snapshots WHERE id=?", from_snapshot)["ref"] if from_snapshot else None
    t0 = time.perf_counter()
    try:
        sb = Sandbox.create(ref, nodes=nodes(), labels={"env": str(env_id)})
    except Exception:
        db.update("envs", "id", env_id, status="failed")
        raise
    db.update("envs", "id", env_id, sandbox=sb.id)
    ready_ms = wait_ready(sb)
    ms = (time.perf_counter() - t0) * 1000
    db.update("envs", "id", env_id, status="running")
    return {"id": env_id, "start_ms": round(ms, 1), "ready_ms": ready_ms}


def app_call(sb: Sandbox, method: str = "GET", path: str = "/") -> dict[str, Any]:
    py = (
        "import urllib.request as u;"
        f"r=u.Request('http://localhost:8000{path}',method='{method}',data=b'' if '{method}'=='POST' else None);"
        "print(u.urlopen(r,timeout=30).read().decode())"
    )
    rc, out = sb.exec(f'python -c "{py}"', timeout=60, workdir="/", node="app", shell="sh")
    if rc:
        raise RuntimeError(out[-500:])
    return json.loads(out.strip().splitlines()[-1])


def wait_ready(sb: Sandbox, timeout: float = 120) -> float:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        try:
            app_call(sb)
            return round((time.perf_counter() - t0) * 1000, 1)
        except Exception:
            time.sleep(0.3)
    raise TimeoutError("env not ready")


def _sandbox(env_id: int) -> Sandbox:
    e = db.one("SELECT sandbox, status FROM envs WHERE id=?", env_id)
    if e["status"] != "running" or not e["sandbox"]:
        raise db.NotFound(f"env {env_id} is {e['status']}")
    return Sandbox.attach(e["sandbox"])


def write(env_id: int, n: int = 10) -> dict[str, Any]:
    return app_call(_sandbox(env_id), "POST", f"/orders/{int(n)}")


def state(env_id: int) -> dict[str, Any]:
    return app_call(_sandbox(env_id))


def snapshot(env_id: int) -> dict[str, Any]:
    sb = _sandbox(env_id)
    t0 = time.perf_counter()
    sb.exec("redis-cli SAVE", workdir="/", node="cache", shell="sh")
    sb.exec("psql -U postgres -c CHECKPOINT", workdir="/", node="db", shell="sh")
    t_quiesce = time.perf_counter()
    ref, _ = sb.snapshot("forge-env-snap", uuid.uuid4().hex[:10])
    t_end = time.perf_counter()
    st = app_call(sb)
    timing = {
        "quiesce_ms": round((t_quiesce - t0) * 1000, 1),
        "frozen_ms": round((t_end - t_quiesce) * 1000, 1),
        "total_ms": round((t_end - t0) * 1000, 1),
    }
    sid = db.insert(
        "env_snapshots",
        env_id=env_id,
        ref=ref,
        state=json.dumps({**st, **timing}),
        ms=timing["total_ms"],
        created=time.time(),
    )
    return {"snapshot": sid, "state": st, **timing}


def destroy(env_id: int) -> None:
    e = db.one("SELECT sandbox FROM envs WHERE id=?", env_id)
    if e["sandbox"]:
        try:
            Sandbox.attach(e["sandbox"]).destroy()
        except LookupError:
            pass  # already gone
    db.update("envs", "id", env_id, status="destroyed")


def list_envs() -> dict[str, list[db.Row]]:
    envs = db.q("SELECT * FROM envs ORDER BY id DESC LIMIT 50")
    snaps = db.q("SELECT id, env_id, state, ms, created FROM env_snapshots ORDER BY id DESC LIMIT 100")
    return {"envs": envs, "snapshots": snaps}


def demo() -> dict[str, Any]:
    """End-to-end: write -> snapshot -> diverge -> restore (state matches snapshot) -> 2 forks are isolated."""
    report = {}
    a = create()
    report["create"] = a
    write(a["id"], 20)
    snap = snapshot(a["id"])
    report["snapshot"] = snap
    write(a["id"], 15)
    report["original_after_more_writes"] = state(a["id"])
    t0 = time.perf_counter()
    with ThreadPoolExecutor(2) as pool:
        forks = list(pool.map(lambda _: create(snap["snapshot"]), range(2)))
    report["parallel_restore_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    report["fork_start"] = forks
    write(forks[0]["id"], 5)
    report["fork_a_state"] = state(forks[0]["id"])
    report["fork_b_state"] = state(forks[1]["id"])
    want = snap["state"]
    report["checks"] = {
        "restore_matches_snapshot": report["fork_b_state"] == want,
        "forks_isolated": report["fork_a_state"]["pg_orders"] == want["pg_orders"] + 5
        and report["fork_b_state"] == want,
        "original_unaffected": report["original_after_more_writes"]["pg_orders"] == want["pg_orders"] + 15,
        "pg_redis_consistent": all(
            s["pg_orders"] == s["redis_counter"] for s in (report["fork_a_state"], report["fork_b_state"])
        ),
    }
    for e in (a, *forks):
        destroy(e["id"])
    db.put_bench("multinode_snapshot", report)
    return report
