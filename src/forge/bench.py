"""Benchmarks that produce the numbers in BENCHMARKS.md."""

import asyncio
import json
import os
import random
import resource
import signal
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from psycopg import sql
from psycopg.conninfo import make_conninfo

from . import bus, config, db, queue


def pct(xs: list[float], p: float) -> float | None:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p / 100 * len(xs)))], 1) if xs else None


# ---------------- scratch databases ----------------


@contextmanager
def scratch_db(name: str) -> Generator[str]:
    """A fresh Postgres database (same server as forge) for a benchmark; dropped afterwards."""
    admin = db.connect(vector=False)  # the server's default database may not be migrated
    admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = make_conninfo(config.DATABASE_URL, dbname=name)
    db.migrate(url)
    try:
        yield url
    finally:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


# Benchmarks announce job ids from scratch databases: they get their own NATS namespace (stream + subjects),
# shared with the worker processes they spawn, so production workers can never consume them.
BENCH_NS = "bench"


@contextmanager
def bench_bus() -> Generator[bus.Bus]:
    b = bus.Bus(namespace=BENCH_NS)
    try:
        yield b
    finally:
        b.drop()


def _count(c: db.Conn, query: str, *args: object) -> int:
    row = c.execute(db.sql(query), args).fetchone()
    assert row is not None
    return row["n"]


# ---------------- chaos: kill -9 workers while they drain a queue ----------------


def chaos(jobs: int = 2000, workers: int = 6, kill_every: float = 0.4, mode: str = "tx") -> dict[str, Any]:
    """SIGKILL random worker processes while they drain a queue; check every job's effect happened exactly once.

    mode=tx: effect committed atomically with completion (exactly-once).
    mode=naive: effect written, then job completed in a separate step (at-least-once => duplicates).
    Jobs are announced over NATS like real work; workers claim them in Postgres."""
    with bench_bus() as the_bus, scratch_db(f"forge_chaos_{mode}") as url, db.connect(url) as c:
        the_bus.purge("chaos")  # drop announcements left over from earlier runs
        now = time.time()
        with c.cursor() as cur:
            cur.executemany(
                db.sql(
                    "INSERT INTO jobs(kind,payload,idem_key,max_attempts,created,updated) VALUES('chaos','{}',?,50,?,?)"
                ),
                [(f"c{i}", now, now) for i in range(jobs)],
            )
        for r in c.execute(db.sql("SELECT id FROM jobs ORDER BY id")).fetchall():
            the_bus.publish_job("chaos", r["id"])
        env = {**os.environ, "FORGE_DATABASE_URL": url, "FORGE_CHAOS_MODE": mode, bus.NAMESPACE_ENV: BENCH_NS}
        cmd = [sys.executable, "-m", "forge.cli", "worker", "--kinds", "chaos", "--concurrency", "16", "--lease", "2"]

        def spawn() -> subprocess.Popen[bytes]:
            return subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        procs = [spawn() for _ in range(workers)]
        kills, t0 = 0, time.time()
        try:
            while _count(c, "SELECT count(*) AS n FROM jobs WHERE status='done'") < jobs and time.time() - t0 < 300:
                time.sleep(kill_every)
                victim = random.randrange(len(procs))
                procs[victim].send_signal(signal.SIGKILL)
                procs[victim].wait()
                kills += 1
                procs[victim] = spawn()
        finally:
            for p in procs:
                p.kill()
        elapsed = time.time() - t0
        table = "effects" if mode == "tx" else "effects_naive"
        done = _count(c, "SELECT count(*) AS n FROM jobs WHERE status='done'")
        effects = _count(c, f"SELECT count(*) AS n FROM {table}")
        distinct = _count(c, f"SELECT count(DISTINCT idem_key) AS n FROM {table}")
        return {
            "mode": mode,
            "jobs": jobs,
            "workers": workers,
            "sigkills": kills,
            "elapsed_s": round(elapsed, 1),
            "jobs_done": done,
            "jobs_retried_after_crash": _count(c, "SELECT count(*) AS n FROM jobs WHERE attempts>1"),
            "effects_written": effects,
            "distinct_effects": distinct,
            "duplicate_effects": effects - distinct,
            "lost": jobs - distinct,
            "throughput_jobs_s": round(done / elapsed, 1),
        }


def throughput(jobs: int = 20_000, workers: int = 4, concurrency: int = 32) -> dict[str, Any]:
    """Queue capacity: no-op jobs announced over NATS, drained by `workers` processes. Measures jobs/s."""
    with bench_bus() as the_bus, scratch_db("forge_throughput") as url, db.connect(url) as c:
        the_bus.purge("noop")
        now = time.time()
        with c.cursor() as cur:
            cur.executemany(
                db.sql("INSERT INTO jobs(kind,payload,idem_key,created,updated) VALUES('noop','{}',?,?,?)"),
                [(f"n{i}", now, now) for i in range(jobs)],
            )
        ids = [r["id"] for r in c.execute("SELECT id FROM jobs ORDER BY id").fetchall()]
        env = {**os.environ, "FORGE_DATABASE_URL": url, bus.NAMESPACE_ENV: BENCH_NS}
        cmd = [sys.executable, "-m", "forge.cli", "worker", "--kinds", "noop", "--concurrency", str(concurrency)]
        procs = [
            subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(workers)
        ]
        try:
            time.sleep(3)  # let workers connect before the clock starts
            t0 = time.time()
            for job_id in ids:
                the_bus.publish_job("noop", job_id)
            while _count(c, "SELECT count(*) AS n FROM jobs WHERE status='done'") < jobs and time.time() - t0 < 300:
                time.sleep(0.2)
            elapsed = time.time() - t0
        finally:
            for p in procs:
                p.terminate()
        done = _count(c, "SELECT count(*) AS n FROM jobs WHERE status='done'")
        return {
            "jobs": jobs,
            "workers": workers,
            "slots": workers * concurrency,
            "done": done,
            "elapsed_s": round(elapsed, 1),
            "jobs_per_s": round(done / elapsed, 1),
        }


def chaos_handler(job: queue.Job) -> dict[str, bool] | tuple[dict[str, bool], Callable[[db.Tx], object]]:
    time.sleep(random.uniform(0.02, 0.25))
    if os.environ.get("FORGE_CHAOS_MODE") == "naive":
        db.ex("INSERT INTO effects_naive(idem_key, job_id) VALUES(?,?)", f"c{job.id}", job.id)
        time.sleep(random.uniform(0, 0.05))  # window in which a crash duplicates the effect
        return {"ok": True}
    return {"ok": True}, lambda c: c.execute(
        "INSERT INTO effects(idem_key,job_id,created) VALUES(?,?,?)", (f"c{job.id}", job.id, time.time())
    )


# ---------------- 100k concurrent simulated agents with crash + resume ----------------


def agents_sim(n: int = 100_000, steps: int = 6, inflight: int = 20_000, crash_after: float = 8.0) -> dict[str, Any]:
    """Each agent: `steps` x (simulated LLM call ~ lognormal latency, then checkpoint (agent, step)).

    Checkpoints are bulk-loaded into Postgres (COPY into a staging table, then an idempotent insert) by a
    single writer. We crash the whole process state mid-run, then resume every agent from its last durable
    checkpoint; the (agent, step) primary key makes every step land exactly once."""
    with scratch_db("forge_agents_sim") as url, db.connect(url) as c:
        c.execute("CREATE UNLOGGED TABLE stage(agent INTEGER, step INTEGER, state TEXT)")

        def flush(batch: list[tuple[int, int, str]]) -> None:
            with c.transaction(), c.cursor() as cur:
                with cur.copy("COPY stage(agent, step, state) FROM STDIN") as copy:
                    for row in batch:
                        copy.write_row(row)
                cur.execute("INSERT INTO sim_checkpoints SELECT * FROM stage ON CONFLICT DO NOTHING")
                cur.execute("TRUNCATE stage")

        async def run(phase_deadline: float) -> dict[str, Any]:
            sem = asyncio.Semaphore(inflight)
            q: asyncio.Queue = asyncio.Queue()
            stats = {"peak": 0, "live": 0, "llm_calls": 0, "writes": 0}
            rows = c.execute("SELECT agent, max(step) AS step FROM sim_checkpoints GROUP BY agent").fetchall()
            resume = {r["agent"]: r["step"] for r in rows}

            async def writer() -> None:
                while True:
                    batch = [await q.get()]
                    while not q.empty() and len(batch) < 20_000:
                        batch.append(q.get_nowait())
                    await asyncio.to_thread(flush, batch)
                    stats["writes"] += len(batch)
                    for _ in batch:
                        q.task_done()

            async def agent(i: int) -> None:
                stats["live"] += 1
                stats["peak"] = max(stats["peak"], stats["live"])
                try:
                    for s in range(resume.get(i, 0) + 1, steps + 1):
                        async with sem:
                            await asyncio.sleep(min(5.0, random.lognormvariate(-0.5, 0.6)))  # "LLM call"
                            stats["llm_calls"] += 1
                        q.put_nowait((i, s, "x" * 16))
                finally:
                    stats["live"] -= 1

            w = asyncio.create_task(writer())
            t0 = time.perf_counter()
            tasks = [asyncio.create_task(agent(i)) for i in range(n)]
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=phase_deadline)
                crashed = False
            except TimeoutError:
                crashed = True  # simulated crash: everything in memory is gone, in-flight steps lost
            if not crashed:
                await q.join()
            w.cancel()
            return {**stats, "crashed": crashed, "elapsed_s": round(time.perf_counter() - t0, 2)}

        rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        phase1 = asyncio.run(run(crash_after))
        durable_after_crash = _count(c, "SELECT count(*) AS n FROM sim_checkpoints")
        phase2 = asyncio.run(run(600))
        total = _count(c, "SELECT count(*) AS n FROM sim_checkpoints")
        complete = _count(
            c, "SELECT count(*) AS n FROM (SELECT agent FROM sim_checkpoints GROUP BY agent HAVING count(*)=?) t", steps
        )
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        scale = 1 if sys.platform == "darwin" else 1024
        return {
            "agents": n,
            "steps_per_agent": steps,
            "inflight_limit": inflight,
            "phase1_crash": phase1,
            "durable_steps_at_crash": durable_after_crash,
            "phase2_resume": phase2,
            "steps_recorded": total,
            "expected_steps": n * steps,
            "agents_complete": complete,
            "exactly_once": total == n * steps and complete == n,
            "peak_rss_mb": round(max(rss, rss0) / scale / 1024 / 1024, 1),
        }


# ---------------- sandbox density / snapshot latency ----------------


def sandboxes(n: int = 40, parallel: int = 16) -> dict[str, Any]:
    from .sandbox import Sandbox

    starts, execs, commits, restores = [], [], [], []

    def one(_: int) -> tuple[float, float, float, float]:
        t0 = time.perf_counter()
        sb = Sandbox.create(config.SANDBOX_IMAGE)
        t1 = time.perf_counter()
        sb.exec("echo hi > /repo/x && python -c 'import pytest'")
        t2 = time.perf_counter()
        img, ms = sb.snapshot("forge-snap", f"bench-{sb.id[:8]}")
        t3 = time.perf_counter()
        sb2 = Sandbox.create(img)
        _, out = sb2.exec("cat /repo/x")
        t4 = time.perf_counter()
        assert out.strip() == "hi"
        sb.destroy()
        sb2.destroy()
        from .sandbox import delete_snapshot

        delete_snapshot(img)
        return (t1 - t0) * 1000, (t2 - t1) * 1000, ms, (t4 - t3) * 1000

    t0 = time.perf_counter()
    with ThreadPoolExecutor(parallel) as pool:
        for s, e, c, r in pool.map(one, range(n)):
            starts.append(s)
            execs.append(e)
            commits.append(c)
            restores.append(r)
    wall = time.perf_counter() - t0
    return {
        "sandboxes": n,
        "parallel": parallel,
        "wall_s": round(wall, 1),
        "sandbox_lifecycles_per_s": round(n / wall, 2),
        "start_ms": {"p50": pct(starts, 50), "p95": pct(starts, 95)},
        "exec_ms": {"p50": pct(execs, 50), "p95": pct(execs, 95)},
        "snapshot_ms": {"p50": pct(commits, 50), "p95": pct(commits, 95)},
        "restore_ms": {"p50": pct(restores, 50), "p95": pct(restores, 95)},
    }


def pipeline_stats() -> dict[str, Any]:
    snaps = [r["commit_ms"] for r in db.q("SELECT commit_ms FROM snapshots WHERE commit_ms IS NOT NULL")]
    tasks = db.q("SELECT status, reject_reason, mutation_score FROM tasks")
    reasons = {}
    for t in tasks:
        if t["status"] == "rejected":
            k = (t["reject_reason"] or "").split(":")[0].split("(")[0].strip()
            reasons[k] = reasons.get(k, 0) + 1
    ms = [t["mutation_score"] for t in tasks if t["mutation_score"] is not None]
    ro = db.q("SELECT kind, parent_id, robust_pass, naive_pass, verdict, steps FROM rollouts WHERE status='graded'")

    def summarize(rs: list[db.Row]) -> dict[str, Any]:
        return {
            "n": len(rs),
            "robust_pass": sum(r["robust_pass"] or 0 for r in rs),
            "naive_pass": sum(r["naive_pass"] or 0 for r in rs),
            "verdicts": {v: sum(1 for r in rs if r["verdict"] == v) for v in ("clean", "suspect", "hack")},
            "mean_steps": round(statistics.mean([r["steps"] or 0 for r in rs]), 1) if rs else None,
        }

    return {
        "tasks": {
            "total": len(tasks),
            "valid": sum(t["status"] == "valid" for t in tasks),
            "rejected": sum(t["status"] == "rejected" for t in tasks),
            "reject_reasons": reasons,
        },
        "mutation_score": {
            "tasks": len(ms),
            "mean": round(statistics.mean(ms), 3) if ms else None,
            "weak_graders(<0.5)": sum(1 for m in ms if m < 0.5),
        },
        "rollouts": {
            "agent_root": summarize([r for r in ro if r["kind"] == "agent" and r["parent_id"] is None]),
            "agent_forks": summarize([r for r in ro if r["kind"] == "agent" and r["parent_id"] is not None]),
            "redteam": summarize([r for r in ro if r["kind"] == "redteam"]),
        },
        "step_snapshot_ms": {"n": len(snaps), "p50": pct(snaps, 50), "p95": pct(snaps, 95)},
    }


def report_md() -> str:
    from . import qa

    b = db.get_bench()
    lines = [
        "# Forge benchmarks",
        "",
        f"_Generated {time.strftime('%Y-%m-%d %H:%M')} on {os.uname().machine}, "
        f"{os.cpu_count()} CPUs. Also served at `/api/bench`._",
        "",
    ]
    sections = [
        ("pipeline", "Pipeline (mine → validate → rollout → QA)"),
        ("hack_corpus", "Reward-hacking corpus: grader robustness + detector"),
        ("throughput", "Queue throughput: no-op jobs over NATS + Postgres"),
        ("chaos_tx", "Chaos: SIGKILL workers, transactional effects"),
        ("chaos_naive", "Chaos: SIGKILL workers, naive effects (baseline)"),
        ("agents_sim", "100k concurrent simulated agents, crash + resume"),
        ("sandboxes", "Sandbox lifecycle / snapshot / restore latency"),
        ("multinode_snapshot", "Multi-node env (app+postgres+redis) group snapshot & fork"),
    ]
    b["pipeline"] = pipeline_stats()
    b["hack_corpus"] = qa.corpus_summary()
    for key, title in sections:
        if key in b:
            data = {k: v for k, v in b[key].items() if k != "_at"}
            lines += [f"## {title}", "", "```json", json.dumps(data, indent=2), "```", ""]
    return "\n".join(lines)
