"""The job queue's guarantees, against real Postgres."""

import json
import threading
import time

import pytest

from forge import queue


def test_enqueue_is_idempotent_and_announced(pg, fake_bus):
    a = queue.enqueue("k", {"x": 1}, key="same")
    b = queue.enqueue("k", {"x": 2}, key="same")
    assert a == b
    assert pg.one("SELECT count(*) AS n FROM jobs")["n"] == 1
    assert fake_bus.published == [("k", a)]
    assert pg.one("SELECT published FROM jobs WHERE id=?", a)["published"] is not None


def test_delayed_jobs_are_announced_by_the_sweeper(pg, fake_bus):
    job = queue.enqueue("k", key="later", delay=0.05)
    assert fake_bus.published == []
    time.sleep(0.1)
    assert queue.sweep(fake_bus)["announced"] == 1
    assert fake_bus.published == [("k", job)]


def test_lease_expiry_and_fencing(pg, fake_bus):
    queue.enqueue("k", key="j1")
    [a] = queue.claim("worker-a", ["k"], lease=0.05)
    assert queue.claim("worker-b", ["k"]) == []  # leased
    time.sleep(0.1)
    [b] = queue.claim("worker-b", ["k"], lease=60)  # expired -> reclaimed
    assert b.id == a.id and b.attempts == 2
    assert not queue.heartbeat(a)  # the zombie loses its heartbeat
    with pytest.raises(queue.LeaseLost):
        queue.complete(a, {"by": "a"})  # and is fenced off
    queue.complete(
        b,
        {"by": "b"},
        effects=lambda c: c.execute("INSERT INTO effects(idem_key, job_id, created) VALUES('e', ?, 0)", (b.id,)),
    )
    row = pg.one("SELECT status, result FROM jobs")
    assert row["status"] == "done" and json.loads(row["result"]) == {"by": "b"}
    assert pg.one("SELECT count(*) AS n FROM effects")["n"] == 1


def test_failures_back_off_then_die(pg, fake_bus):
    queue.enqueue("k", key="j", max_attempts=1)
    [j] = queue.claim("w", ["k"])
    queue.fail(j, "boom")
    assert pg.one("SELECT status FROM jobs")["status"] == "dead"


def test_claim_ids_skips_jobs_that_are_taken(pg, fake_bus):
    ids = [queue.enqueue("k", key=f"j{i}") for i in range(3)]
    [first] = queue.claim_ids("a", [ids[0]], ["k"])
    taken = queue.claim_ids("b", ids, ["k"])  # a duplicate announcement for ids[0] must not double-claim it
    assert first.id == ids[0]
    assert sorted(j.id for j in taken) == ids[1:]


def test_concurrent_workers_never_claim_the_same_job(pg, fake_bus):
    """SKIP LOCKED under contention: 8 threads race to drain 300 jobs."""
    for i in range(300):
        queue.enqueue("k", key=f"j{i}")
    claimed: list[int] = []
    lock = threading.Lock()

    def drain(owner):
        while batch := queue.claim(owner, ["k"], n=7):
            with lock:
                claimed.extend(j.id for j in batch)

    threads = [threading.Thread(target=drain, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 300 and len(set(claimed)) == 300


def test_released_jobs_are_reclaimable_immediately(pg, fake_bus):
    queue.enqueue("k", key="j")
    [j] = queue.claim("a", ["k"], lease=3600)
    queue.release(j)
    [again] = queue.claim("b", ["k"])
    assert again.id == j.id and again.owner == "b"


def test_claim_ids_skips_other_kinds(pg, fake_bus):
    """A mislabelled announcement must not hand a worker a job of a kind it has no handler for."""
    other = queue.enqueue("other", key="o")
    assert queue.claim_ids("a", [other], ["k"]) == []
    assert pg.one("SELECT status FROM jobs WHERE id=?", other)["status"] == "queued"


def test_retry_refuses_queued_or_running_jobs(pg, fake_bus):
    job_id = queue.enqueue("k", key="j", max_attempts=1)
    assert not queue.retry(job_id)  # queued
    [j] = queue.claim("a", ["k"])
    assert not queue.retry(job_id)  # running: retrying would run it twice
    queue.fail(j, "boom")
    assert queue.retry(job_id)  # dead -> queued, with a fresh budget but monotonic attempts
    [again] = queue.claim("b", ["k"])
    assert again.attempts == 2  # the fencing token can't collide with the first run's
    row = pg.one("SELECT max_attempts FROM jobs")
    assert row["max_attempts"] == 1 + queue.RETRY_BUDGET


def test_release_on_last_attempt_is_not_killed_by_sweep(pg, fake_bus):
    queue.enqueue("k", key="j", max_attempts=1)
    [j] = queue.claim("a", ["k"], lease=3600)
    queue.release(j)
    assert queue.sweep(fake_bus)["dead"] == 0
    row = pg.one("SELECT status, attempts FROM jobs")
    assert row["status"] == "queued" and row["attempts"] == 0
    [again] = queue.claim("b", ["k"])
    assert again.attempts == 1
    with pytest.raises(queue.LeaseLost):
        queue.complete(j, {})  # the old owner is fenced off despite the same attempt number


def test_expired_lease_past_max_attempts_is_not_reclaimed(pg, fake_bus):
    queue.enqueue("k", key="j", max_attempts=1)
    [j] = queue.claim("a", ["k"], lease=0.05)
    time.sleep(0.1)
    assert queue.claim("b", ["k"]) == []
    assert queue.claim_ids("b", [j.id], ["k"]) == []
    assert queue.sweep(fake_bus)["dead"] == 1


def test_complete_rolls_back_when_effects_fail(pg, fake_bus):
    queue.enqueue("k", key="j")
    [j] = queue.claim("a", ["k"])

    def effects(c):
        c.execute("INSERT INTO effects(idem_key, job_id, created) VALUES('e', ?, 0)", (j.id,))
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        queue.complete(j, {"ok": True}, effects=effects)
    assert pg.one("SELECT status FROM jobs")["status"] == "running"
    assert pg.one("SELECT count(*) AS n FROM effects")["n"] == 0


def test_reannouncements_reuse_a_message_id(pg, fake_bus, monkeypatch):
    seen: list[str | None] = []
    monkeypatch.setattr(fake_bus, "publish_job", lambda kind, job_id, msg_id=None: seen.append(msg_id) or True)
    queue.enqueue("k", key="j")
    pg.ex("UPDATE jobs SET published=0")
    queue.sweep(fake_bus)
    assert len(seen) == 2 and seen[0] == seen[1]  # NATS dedupes the second within its duplicate window


def test_sweep_stops_announcing_when_the_bus_is_down(pg, fake_bus, monkeypatch):
    for i in range(5):
        queue.enqueue("k", key=f"j{i}", delay=0.01)
    calls = []
    monkeypatch.setattr(fake_bus, "publish_job", lambda *a: calls.append(a) and False)
    time.sleep(0.02)
    assert queue.sweep(fake_bus)["announced"] == 0
    assert len(calls) == 1


def test_rollout_creation_is_idempotent(pg, fake_bus, monkeypatch):
    from forge import agent

    a = agent.create_rollout("t", key="task:t:agent")
    b = agent.create_rollout("t", key="task:t:agent")
    assert a == b
    forks = agent.fork(a, 2, 2)
    assert agent.fork(a, 2, 3)[:2] == forks  # asking again converges; a larger n only adds
    assert pg.one("SELECT count(*) AS n FROM rollouts")["n"] == 4
    jobs = pg.q("SELECT idem_key FROM jobs WHERE kind='rollout'")
    assert len(jobs) == 4  # every rollout has exactly one job


def test_migrations_are_idempotent(pg, pg_url):
    assert pg.migrate(pg_url) == []
