"""NATS JetStream delivery (skipped if no NATS server is reachable)."""

import time
import uuid

import pytest

from forge import bus


@pytest.fixture()
def nats_bus():
    b = bus.Bus(namespace="test")  # own stream: never touches production announcements
    if not b.connected():
        pytest.skip("NATS not reachable (run `mise run infra`)")
    yield b
    b.drop()  # deletes the stream and every durable consumer on it


def test_announced_jobs_reach_a_worker_once(nats_bus):
    kind = f"test{uuid.uuid4().hex[:8]}"
    for job_id in (1, 2, 3):
        assert nats_bus.publish_job(kind, job_id)
    got = nats_bus.fetch([kind], n=10, timeout=2)
    assert sorted(j for _, j, _ in got) == [1, 2, 3]
    for _, _, msg in got:
        nats_bus.ack(msg)
    assert nats_bus.fetch([kind], n=10, timeout=0.5) == []  # acked work is gone (work-queue stream)


def test_workers_with_overlapping_kinds_share_per_kind_consumers(nats_bus):
    a, b = f"a{uuid.uuid4().hex[:6]}", f"b{uuid.uuid4().hex[:6]}"
    nats_bus.publish_job(a, 1)
    nats_bus.publish_job(b, 2)
    first = nats_bus.fetch([a, b], n=1, timeout=2)  # respects n across kinds
    assert len(first) == 1
    rest = nats_bus.fetch([a, b], n=5, timeout=2)  # the other kind's poll was left running, not lost
    assert sorted(j for _, j, _ in first + rest) == [1, 2]


def test_duplicate_announcements_are_dropped(nats_bus):
    kind = f"dup{uuid.uuid4().hex[:6]}"
    assert nats_bus.publish_job(kind, 7, msg_id="7:0:x")
    assert nats_bus.publish_job(kind, 7, msg_id="7:0:x")
    got = nats_bus.fetch([kind], n=10, timeout=1)
    assert [j for _, j, _ in got] == [7]


def test_events_are_delivered_to_subscribers(nats_bus):
    import threading

    seen: list[dict] = []
    done = threading.Event()
    unsubscribe = nats_bus.subscribe_events(lambda e: (seen.append(e), done.set()))
    nats_bus.event(kind="probe", value=42)
    assert done.wait(2)
    unsubscribe()
    assert seen[0] == {"kind": "probe", "value": 42}


def test_unreachable_nats_fails_fast_and_stays_fast():
    b = bus.Bus("nats://127.0.0.1:1", namespace="test")
    t0 = time.monotonic()
    assert not b.connected()
    assert time.monotonic() - t0 < 3
    t1 = time.monotonic()
    for _ in range(20):  # circuit breaker: no reconnect attempt per call
        assert not b.publish_job("k", 1)
        assert b.fetch(["k"], 1) == []
    assert time.monotonic() - t1 < 0.5
