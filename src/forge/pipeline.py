"""Job handlers + pipeline wiring. Every handler is idempotent: safe to re-run after a crash.

mine_repo -> (index_repo, validate_task*) ; validate_task[valid] -> (hack_corpus, mutate, rollout agent, rollout redteam)
rollout -> grade ; grade[failed root] -> fork x N -> rollout ...
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from . import agent, bench, bus, config, db, index, mine, mutate, qa, queue, validate
from .worker import SWEEP_EVERY, handler

log = logging.getLogger("forge.pipeline")

# priorities: cheap/unblocking work first
P_MINE, P_VALIDATE, P_GRADE, P_QA, P_INDEX, P_ROLLOUT = 9, 8, 6, 4, 3, 1


@handler("mine_repo")
def h_mine(job: queue.Job) -> dict[str, Any]:
    name = job.payload["repo"]
    _, head, new = mine.mine_repo(name, config.REPOS[name])
    queue.enqueue("index_repo", {"repo": name}, key=f"index:{name}:{head}", priority=P_INDEX)
    for tid in new:
        queue.enqueue("validate_task", {"task": tid}, key=f"validate:{tid}", priority=P_VALIDATE)
    return {"head": head, "new_tasks": new}


@handler("index_repo")
def h_index(job: queue.Job) -> dict[str, Any]:
    return index.index_repo(job.payload["repo"], job)


@handler("validate_task")
def h_validate(job: queue.Job) -> dict[str, Any]:
    tid = job.payload["task"]
    res = validate.validate(tid, job)
    if res["status"] == "valid":
        qa.enqueue_task_qa(tid)
        # keyed, so a re-run after a crash between the two creates the missing one instead of skipping it
        agent.create_rollout(tid, "agent", priority=P_ROLLOUT, key=f"task:{tid}:agent")
        agent.create_rollout(tid, "redteam", priority=P_ROLLOUT, temperature=0.5, key=f"task:{tid}:redteam")
    return res


@handler("rollout")
def h_rollout(job: queue.Job) -> dict[str, Any]:
    return agent.rollout(job.payload["rollout"], job)


@handler("grade")
def h_grade(job: queue.Job) -> dict[str, Any]:
    return qa.grade(job.payload["rollout"])


@handler("hack_corpus")
def h_hack(job: queue.Job) -> dict[str, Any]:
    return qa.hack_corpus(job.payload["task"])


@handler("mutate")
def h_mutate(job: queue.Job) -> dict[str, Any]:
    return mutate.mutate_task(job.payload["task"])


@handler("chaos")
def h_chaos(job: queue.Job) -> dict[str, Any] | tuple[dict[str, Any], Callable[[db.Tx], object]]:
    return bench.chaos_handler(job)


@handler("noop")
def h_noop(job: queue.Job) -> dict[str, Any]:
    """Benchmark job: measures queue overhead only."""
    return {"ok": True}


def start_pipeline(repos: list[str] | None = None) -> None:
    for name in repos or config.REPOS:
        queue.enqueue(
            "mine_repo",
            {"repo": name},
            key=f"mine:{name}:{int(time.time() // config.REFRESH_SECONDS)}",
            priority=P_MINE,
        )


def scheduler(stop: threading.Event) -> None:
    """Runs in every API replica (duplicate work is harmless: enqueues are idempotent, sweeps are locked).

    - every few seconds: announce queued jobs that never reached NATS (outbox sweep)
    - every minute: queue a refresh of every known repo once per REFRESH_SECONDS (mining + incremental index)
    """
    last_refresh = 0.0
    while not stop.wait(SWEEP_EVERY):
        try:
            queue.sweep(bus.get())
            if time.time() - last_refresh > 60:
                last_refresh = time.time()
                known = [r["name"] for r in db.q("SELECT name FROM repos") if r["name"] in config.REPOS]
                if known:
                    start_pipeline(known)
        except Exception:
            log.exception("scheduler tick failed")


# LLM-bound kinds share FORGE_LLM_CONCURRENCY slots; cap them so sandbox-only work keeps flowing
# rollout cap == ollama slots: more concurrent agents than slots thrashes the per-slot KV prefix cache
KIND_LIMITS = {
    "validate_task": 3,
    "rollout": config.LLM_CONCURRENCY,
    "grade": 2,
    "index_repo": 1,
    "hack_corpus": 2,
    "mutate": 2,
}

PIPELINE_KINDS = ["mine_repo", "index_repo", "validate_task", "rollout", "grade", "hack_corpus", "mutate"]
