"""Durable job queue: Postgres is the source of truth, NATS only announces work.

- Leases: a claimed job is owned until `lease_expires`; an expired lease is re-claimable (crash recovery).
- Fencing: (lease_owner, attempts) is the fencing token. Heartbeats and completions from a stale
  owner are rejected, so a zombie worker can never overwrite the result of the worker that replaced it.
- Exactly-once effects: `complete(..., effects=fn)` runs fn inside the same transaction that flips the
  job to done, conditional on still holding the lease.
- Outbox: a job is committed first, then announced on NATS. `sweep()` re-announces anything still
  queued, so a crash between the two steps (or a lost message) only delays a job, never drops it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import db

if TYPE_CHECKING:
    from .bus import Bus

_SWEEP_LOCK = 4243  # pg advisory lock id: only one process sweeps at a time
REANNOUNCE_AFTER = 30.0  # seconds a queued job may sit unclaimed before it is published again
RETRY_BUDGET = 3  # attempts a manual retry grants on top of those already used


class LeaseLost(Exception):
    pass


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int
    owner: str
    lost: bool = field(default=False)

    def check(self) -> None:
        if self.lost:
            raise LeaseLost(f"job {self.id} lease lost")


def _announce(bus: Bus | None, kind: str, job_id: int, attempts: int = 0) -> bool:
    if bus is None:
        return False
    from .bus import DEDUP_WINDOW

    # same id within a window: NATS drops re-announcements of a job that's still waiting in the stream
    msg_id = f"{job_id}:{attempts}:{int(time.time() // DEDUP_WINDOW)}"
    if not bus.publish_job(kind, job_id, msg_id):
        return False
    db.ex("UPDATE jobs SET published=? WHERE id=?", time.time(), job_id)
    return True


def enqueue(
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    key: str | None = None,
    priority: int = 0,
    max_attempts: int = 3,
    delay: float = 0.0,
    bus: Bus | None = None,
) -> int:
    """Insert a job (idempotent on `key`) and announce it. Returns the job id."""
    if bus is None:
        from . import bus as bus_mod

        bus = bus_mod.get()
    now = time.time()
    row = db.q1(
        "INSERT INTO jobs(kind,payload,idem_key,priority,max_attempts,run_after,created,updated)"
        " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(idem_key) DO NOTHING RETURNING id",
        kind,
        json.dumps(payload or {}),
        key,
        priority,
        max_attempts,
        now + delay,
        now,
        now,
    )
    if row is None:  # already exists
        existing = db.q1("SELECT id FROM jobs WHERE idem_key=?", key)
        assert existing is not None
        return existing["id"]
    if not delay:
        _announce(bus, kind, row["id"])
    return row["id"]


_CLAIM = """UPDATE jobs SET status='running', lease_owner=?, lease_expires=?, attempts=attempts+1, updated=?
            WHERE id IN ({select}) RETURNING id, kind, payload, attempts"""
# an expired lease is only re-claimable while attempts remain; past that, sweep() marks the job dead
_CLAIMABLE = "run_after<=? AND (status='queued' OR (status='running' AND lease_expires<? AND attempts<max_attempts))"


def _jobs(rows: Iterable[db.Row], owner: str) -> list[Job]:
    return [Job(r["id"], r["kind"], json.loads(r["payload"]), r["attempts"], owner) for r in rows]


def claim(owner: str, kinds: list[str], n: int = 1, lease: float = 60.0) -> list[Job]:
    """Claim up to n jobs straight from Postgres (highest priority first). Safe with many concurrent workers."""
    now = time.time()
    select = (
        f"SELECT id FROM jobs WHERE kind = ANY(?) AND {_CLAIMABLE} "
        "ORDER BY priority DESC, id LIMIT ? FOR UPDATE SKIP LOCKED"
    )
    with db.tx() as c:
        rows = c.execute(_CLAIM.format(select=select), (owner, now + lease, now, kinds, now, now, n)).fetchall()
    return _jobs(rows, owner)


def claim_ids(owner: str, ids: list[int], kinds: list[str], lease: float = 60.0) -> list[Job]:
    """Claim specific jobs announced on NATS. Ids that are already taken or done, or that aren't one of
    `kinds` (a mislabelled announcement must not hand a worker a job it has no handler for), are skipped."""
    if not ids or not kinds:
        return []
    now = time.time()
    select = f"SELECT id FROM jobs WHERE id = ANY(?) AND kind = ANY(?) AND {_CLAIMABLE} FOR UPDATE SKIP LOCKED"
    with db.tx() as c:
        rows = c.execute(_CLAIM.format(select=select), (owner, now + lease, now, ids, kinds, now, now)).fetchall()
    return _jobs(rows, owner)


def heartbeat(job: Job, lease: float = 60.0) -> bool:
    renewed = db.ex(
        "UPDATE jobs SET lease_expires=?, updated=? WHERE id=? AND lease_owner=? AND attempts=? AND status='running'",
        time.time() + lease,
        time.time(),
        job.id,
        job.owner,
        job.attempts,
    )
    if renewed == 0:
        job.lost = True
    return not job.lost


def complete(job: Job, result: Any = None, effects: Callable[[db.Tx], object] | None = None) -> None:
    with db.tx() as c:
        cur = c.execute(
            "UPDATE jobs SET status='done', result=?, error=NULL, updated=?"
            " WHERE id=? AND lease_owner=? AND attempts=? AND status='running'",
            (json.dumps(result), time.time(), job.id, job.owner, job.attempts),
        )
        if cur.rowcount == 0:
            raise LeaseLost(f"job {job.id}: completion fenced off")
        if effects:
            effects(c)


def fail(job: Job, error: str) -> None:
    """Requeue with exponential backoff, or mark dead after max_attempts. The sweeper re-announces it."""
    db.ex(
        "UPDATE jobs SET status = CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'queued' END,"
        " error=?, lease_owner=NULL, run_after=?, published=NULL, updated=?"
        " WHERE id=? AND lease_owner=? AND attempts=?",
        error[-4000:],
        time.time() + min(60, 2**job.attempts),
        time.time(),
        job.id,
        job.owner,
        job.attempts,
    )


def release(job: Job) -> None:
    """Give a running job back (graceful shutdown) for any worker to claim now, refunding the attempt: being
    handed back isn't a failure, and on its last attempt the sweeper would otherwise mark it dead.

    The next claim reuses this attempt number, but under a different owner, so fencing still holds."""
    db.ex(
        "UPDATE jobs SET status='queued', attempts=attempts-1, lease_owner=NULL, lease_expires=NULL,"
        " published=NULL, updated=? WHERE id=? AND lease_owner=? AND attempts=? AND status='running'",
        time.time(),
        job.id,
        job.owner,
        job.attempts,
    )


def retry(job_id: int) -> bool:
    """Re-run a finished (dead or done) job; False if it's queued or running. Attempts stay monotonic -- they
    are half of the fencing token -- so the budget is raised instead of reset."""
    return (
        db.ex(
            "UPDATE jobs SET status='queued', max_attempts=attempts+?, run_after=0, error=NULL, lease_owner=NULL,"
            " lease_expires=NULL, published=NULL, updated=? WHERE id=? AND status IN ('dead','done')",
            RETRY_BUDGET,
            time.time(),
            job_id,
        )
        > 0
    )


def sweep(bus: Bus | None, limit: int = 1000) -> dict[str, int]:
    """Housekeeping, run periodically by every process; an advisory lock lets only one do it at a time.

    Expired leases whose attempts are used up become dead; runnable jobs that were never announced
    (crash after commit, NATS down, backoff elapsed) or announced long ago are announced again."""
    now = time.time()
    with db.tx() as c:
        if not c.one("SELECT pg_try_advisory_xact_lock(?) AS ok", (_SWEEP_LOCK,))["ok"]:
            return {"skipped": 1}
        dead = c.execute(
            "UPDATE jobs SET status='dead', error=coalesce(error,'lease expired'), updated=?"
            " WHERE status='running' AND lease_expires<? AND attempts>=max_attempts",
            (now, now),
        ).rowcount
        due = c.execute(
            f"SELECT id, kind, attempts FROM jobs WHERE {_CLAIMABLE} AND (published IS NULL OR published<?)"
            " ORDER BY priority DESC, id LIMIT ?",
            (now, now, now - REANNOUNCE_AFTER, limit),
        ).fetchall()
    announced = 0
    for r in due:
        if not _announce(bus, r["kind"], r["id"], r["attempts"]):
            break  # NATS is down: the rest would fail too (and each failure costs time); next sweep retries
        announced += 1
    return {"dead": dead, "announced": announced}


def stats() -> dict[str, dict[str, int]]:
    # a 'running' job whose lease expired has no live owner: report it as queued (it will be reclaimed)
    rows = db.q(
        "SELECT kind, CASE WHEN status='running' AND lease_expires<? THEN 'queued' ELSE status END AS status,"
        " count(*) AS n FROM jobs GROUP BY 1, 2",
        time.time(),
    )
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["kind"], {})[r["status"]] = r["n"]
    return out
