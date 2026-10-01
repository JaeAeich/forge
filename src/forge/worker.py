"""Worker: takes job announcements from NATS, claims them in Postgres, runs handlers on threads.

Run as many worker processes (on as many machines) as you like; they coordinate only through
Postgres claims, so a job never runs twice. If NATS is unreachable they claim straight from Postgres.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import traceback
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any

from . import bus as bus_mod
from . import db, queue

log = logging.getLogger("forge.worker")
Handler = Callable[[queue.Job], Any]
HANDLERS: dict[str, Handler] = {}

PRESENCE_EVERY = 3.0  # seconds between worker-presence updates (and reads of the shared pause flag)
POLL_FALLBACK = 2.0  # seconds between direct Postgres claims (catches anything NATS didn't deliver)
SWEEP_EVERY = 5.0
PRESENCE_TTL = 3600.0  # presence rows of workers silent this long (killed, never shut down) are deleted


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


class Worker:
    def __init__(
        self,
        kinds: list[str] | None = None,
        concurrency: int = 4,
        lease: float = 60.0,
        limits: dict[str, int] | None = None,
        use_bus: bool = True,
    ):
        self.kinds = kinds or list(HANDLERS)
        self.limits = limits or {}  # per-kind caps so LLM-bound kinds can't starve docker/CPU-bound ones
        self.concurrency = concurrency
        self.lease = lease
        self.bus = bus_mod.get() if use_bus else None
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.active: dict[int, queue.Job] = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.paused = False  # cluster-wide switch (control table): stop claiming new work, let in-flight jobs finish

    # ---------- running jobs ----------
    def _run_job(self, job: queue.Job) -> None:
        status = "done"
        try:
            result = HANDLERS[job.kind](job)
            if isinstance(result, tuple):  # (result, effects_fn)
                queue.complete(job, result[0], effects=result[1])
            else:
                queue.complete(job, result)
        except queue.LeaseLost as e:
            status = "lost"
            log.warning("%s", e)
        except Exception as e:
            status = "failed"
            log.error("job %s %s failed: %s", job.id, job.kind, e)
            queue.fail(job, f"{e}\n{traceback.format_exc()}")
        finally:
            with self.lock:
                self.active.pop(job.id, None)
            if self.bus:
                self.bus.event(job=job.id, kind=job.kind, status=status, payload=job.payload)

    def _start(self, jobs: list[queue.Job]) -> None:
        for j in jobs:
            with self.lock:
                self.active[j.id] = j
            threading.Thread(target=self._run_job, args=(j,), daemon=True, name=f"job-{j.id}").start()

    def _heartbeat(self) -> None:
        while not self.stop.wait(self.lease / 4):
            with self.lock:
                jobs = list(self.active.values())
            for j in jobs:
                try:
                    queue.heartbeat(j, self.lease)
                except Exception as e:  # transient db error; the next beat retries
                    log.warning("heartbeat: %s", e)

    def _presence(self) -> None:
        """Advertise this worker (for the UI) and pick up the cluster-wide pause switch."""
        while True:
            with self.lock:
                active = [
                    {"id": j.id, "kind": j.kind, "payload": j.payload, "attempts": j.attempts}
                    for j in self.active.values()
                ]
            try:
                db.ex(
                    "INSERT INTO workers(owner,concurrency,active,seen) VALUES(?,?,?,?) ON CONFLICT(owner) DO UPDATE"
                    " SET concurrency=excluded.concurrency, active=excluded.active, seen=excluded.seen",
                    self.owner,
                    self.concurrency,
                    json.dumps(active),
                    time.time(),
                )
                db.ex("DELETE FROM workers WHERE seen < ?", time.time() - PRESENCE_TTL)
                self.paused = db.get_control("paused") == "1"
            except Exception as e:
                log.warning("presence: %s", e)
            if self.stop.wait(PRESENCE_EVERY):
                return

    # ---------- claiming ----------
    def _capacity(self) -> dict[str, int]:
        """How many more jobs of each kind this worker may start right now."""
        with self.lock:
            free = self.concurrency - len(self.active)
            running = Counter(j.kind for j in self.active.values())
        if free <= 0:
            return {}
        caps = {k: min(free, self.limits.get(k, self.concurrency) - running[k]) for k in self.kinds}
        return {k: c for k, c in caps.items() if c > 0}

    def _from_bus(self, room: dict[str, int]) -> list[queue.Job]:
        assert self.bus is not None
        kinds = list(room)
        msgs = self.bus.fetch(kinds, sum(room.values()), timeout=1.0)  # only kinds with room
        take: list[int] = []
        for kind, job_id, msg in msgs:
            if room.get(kind, 0) > 0:
                room[kind] -= 1
                take.append(job_id)
                self.bus.ack(msg)  # the Postgres claim below decides; the lease covers crashes from here
            else:
                self.bus.nak(msg, delay=1.0)  # at this kind's cap: let another worker have it
        return queue.claim_ids(self.owner, take, kinds, self.lease)

    def run(self) -> None:
        self.paused = db.get_control("paused") == "1"  # honour the switch before claiming anything
        threading.Thread(target=self._heartbeat, daemon=True, name="heartbeat").start()
        threading.Thread(target=self._presence, daemon=True, name="presence").start()
        last_poll = last_sweep = 0.0
        while not self.stop.is_set():
            if self.paused:
                time.sleep(0.5)
                continue
            room = self._capacity()
            jobs: list[queue.Job] = []
            try:
                if room and self.bus and self.bus.connected():
                    jobs = self._from_bus(dict(room))
                    room = self._capacity()
                if room and time.time() - last_poll > POLL_FALLBACK:
                    last_poll = time.time()
                    # one at a time when caps apply, so a single claim can't overshoot a kind's cap
                    jobs += queue.claim(self.owner, list(room), 1 if self.limits else max(room.values()), self.lease)
                if time.time() - last_sweep > SWEEP_EVERY:
                    last_sweep = time.time()
                    queue.sweep(self.bus)
            except Exception as e:
                log.warning("claim loop: %s", e)
                time.sleep(1)
            self._start(jobs)
            if not jobs and not (self.bus and self.bus.connected()):
                time.sleep(0.2)
            elif not room:
                time.sleep(0.05)

    def shutdown(self, grace: float) -> None:
        """Stop claiming, give running jobs `grace` seconds, then hand the rest back for immediate re-claim.

        Handed-back rollouts resume from their last step snapshot on another worker, so little work is lost."""
        self.stop.set()
        deadline = time.time() + grace
        while time.time() < deadline:
            with self.lock:
                if not self.active:
                    break
            time.sleep(0.5)
        with self.lock:
            leftover = list(self.active.values())
        for job in leftover:
            queue.release(job)
        db.ex("DELETE FROM workers WHERE owner=?", self.owner)
        log.info("worker stopped", extra={"handed_back": len(leftover)})

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="worker")
        t.start()
        return t
