"""NATS: hands out jobs to workers (JetStream) and broadcasts live events to the UI (core NATS).

Postgres stays the source of truth. A NATS message only says "job N is ready"; the worker still has to
claim job N in Postgres, so lost or duplicated messages can never run a job twice or lose it
(a sweeper re-publishes anything that sits queued). Every call here fails soft and fast: if NATS is down,
callers get False/[] at once (a circuit breaker stops reconnect attempts for a while) and workers fall back
to claiming straight from Postgres.

Subjects and the stream live in a namespace (FORGE_BUS_NAMESPACE; empty = production) so benchmarks and
tests, whose job ids point into scratch databases, can never be consumed by production workers.

nats-py is asyncio-only; this module runs it on a private event-loop thread behind a sync API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any

import nats
import nats.errors
import nats.js.errors
from nats.js.api import ConsumerConfig, DiscardPolicy, RetentionPolicy, StreamConfig

from . import config

log = logging.getLogger("forge.bus")

STREAM = "FORGE_JOBS"
NAMESPACE_ENV = "FORGE_BUS_NAMESPACE"
TIMEOUT = 5.0
CONNECT_TIMEOUT = 2.0
RETRY_AFTER = 10.0  # after a failed connect, report "down" without trying again for this long
# Publishes carry a Nats-Msg-Id that only changes once per window, so the sweeper's periodic re-announcements
# of a job nobody has picked up yet are dropped by the server instead of piling up (must be >= REANNOUNCE_AFTER).
DEDUP_WINDOW = 120.0
# Backstop for a stuck backlog: at most this many announcements per kind; the oldest are discarded first.
# Dropping one is harmless -- the job is still queued in Postgres and gets re-announced or polled.
MAX_PER_KIND = 100_000
_LEGACY_CONSUMER = re.compile(r"^w-[0-9a-f]{12}$")  # old one-per-kind-set consumers; they overlap per-kind ones


def _namespace() -> str:
    return re.sub(r"[^a-z0-9_]", "_", os.environ.get(NAMESPACE_ENV, "").lower())


def consumer_name(kind: str) -> str:
    return f"jobs-{kind}"


class Bus:
    def __init__(self, url: str = config.NATS_URL, namespace: str | None = None):
        ns = _namespace() if namespace is None else namespace
        self.namespace = ns
        self.stream = f"{STREAM}_{ns.upper()}" if ns else STREAM
        self.jobs = f"forge.{ns}.jobs" if ns else "forge.jobs"
        self.events = f"forge.{ns}.events" if ns else "forge.events"
        self.url = url
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="nats").start()
        self.nc: Any = None
        self.js: Any = None
        self.subs: dict[str, Any] = {}
        self.polls: dict[str, asyncio.Future] = {}  # per-kind long-polls still running from an earlier fetch
        self.lock = threading.Lock()
        self.down_until = 0.0

    def _run(self, coro: Coroutine[Any, Any, Any], timeout: float = TIMEOUT) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except BaseException:
            fut.cancel()  # don't leave it running on the bus loop after we gave up waiting
            raise

    # ---------- connection ----------
    def connected(self) -> bool:
        """True if NATS is usable right now. Never blocks for long and never stacks connection attempts."""
        nc = self.nc
        if nc is not None and not nc.is_closed:
            return nc.is_connected  # reconnecting counts as "not now"; nats-py keeps reconnecting by itself
        if time.monotonic() < self.down_until:
            return False
        with self.lock:
            if self.nc is not None and not self.nc.is_closed:
                return self.nc.is_connected
            if time.monotonic() < self.down_until:
                return False
            try:
                self._run(self._connect())
                return True
            except Exception as e:
                self.down_until = time.monotonic() + RETRY_AFTER
                log.warning("nats unavailable (%s); falling back to Postgres polling", e or type(e).__name__)
                return False

    async def _connect(self) -> None:
        # The first connect gives up after a quick retry (fail fast); once up, nats-py reconnects forever by
        # itself and restores subscriptions.
        nc = await nats.connect(
            self.url, connect_timeout=CONNECT_TIMEOUT, max_reconnect_attempts=1, reconnect_time_wait=0.1
        )
        try:
            js = nc.jetstream()
            await self._ensure_stream(js)
        except BaseException:
            await nc.close()
            raise
        nc.options["max_reconnect_attempts"] = -1
        nc.options["reconnect_time_wait"] = 2.0
        self.subs, self.polls = {}, {}
        self.nc, self.js = nc, js

    async def _ensure_stream(self, js: Any) -> None:
        """Create the stream if it's missing; never update it (in Kubernetes it is declared by NACK)."""
        try:
            await js.stream_info(self.stream)
        except nats.js.errors.NotFoundError:
            cfg = StreamConfig(
                name=self.stream,
                subjects=[f"{self.jobs}.>"],
                retention=RetentionPolicy.WORK_QUEUE,
                duplicate_window=DEDUP_WINDOW,
                max_msgs_per_subject=MAX_PER_KIND,
                discard=DiscardPolicy.OLD,
            )
            await js.add_stream(cfg)
            return
        for c in await js.consumers_info(self.stream):
            if _LEGACY_CONSUMER.match(c.name):
                await js.delete_consumer(self.stream, c.name)

    def drop(self) -> None:
        """Delete this namespace's stream (benchmarks/tests clean up after themselves). Never production's."""
        assert self.namespace, "refusing to delete the production stream"
        if self.connected():
            self._soft(self.js.delete_stream(self.stream))
            self.subs, self.polls = {}, {}

    # ---------- jobs ----------
    def publish_job(self, kind: str, job_id: int, msg_id: str | None = None) -> bool:
        """Announce a job. `msg_id` lets the server drop duplicates within DEDUP_WINDOW."""
        if not self.connected():
            return False
        headers = {"Nats-Msg-Id": msg_id} if msg_id else None
        try:
            self._run(self.js.publish(f"{self.jobs}.{kind}", str(job_id).encode(), headers=headers))
            return True
        except Exception as e:
            log.warning("publish job %s: %s", job_id, e)
            return False

    def purge(self, kind: str) -> None:
        """Drop pending announcements for one kind (used by benchmarks between runs)."""
        if self.connected():
            self._soft(self.js.purge_stream(self.stream, subject=f"{self.jobs}.{kind}"))

    def fetch(self, kinds: list[str], n: int, timeout: float = 1.0) -> list[tuple[str, int, Any]]:
        """Up to n ready job announcements for these kinds: [(kind, job_id, msg)]. Caller must ack/nak."""
        if n <= 0 or not kinds or not self.connected():
            return []
        try:
            return self._run(self._fetch(kinds, n, timeout), timeout + TIMEOUT)
        except Exception as e:
            log.warning("fetch: %s", e)
            return []

    async def _sub(self, kind: str) -> Any:
        # One durable pull consumer per kind, shared by every worker that runs that kind (load balancing).
        # Per kind, not per kind-set: filtered consumers on a work-queue stream must not overlap.
        sub = self.subs.get(kind)
        if sub is None:
            name = consumer_name(kind)
            cfg = ConsumerConfig(ack_wait=60, max_ack_pending=-1)  # filter_subject comes from the subject
            subject = f"{self.jobs}.{kind}"
            sub = self.subs[kind] = await self.js.pull_subscribe(subject, durable=name, stream=self.stream, config=cfg)
        return sub

    async def _fetch(self, kinds: list[str], n: int, timeout: float) -> list[tuple[str, int, Any]]:
        async def poll(kind: str) -> list[Any]:
            try:
                return await (await self._sub(kind)).fetch(n, timeout=timeout)
            except nats.errors.TimeoutError:
                return []

        # Long-poll every kind at once and return as soon as any has work. The others are left running, not
        # cancelled (a cancelled pull can swallow a delivered message until ack_wait): the next fetch of that
        # kind picks up their result.
        polls = {k: self.polls.pop(k, None) or asyncio.ensure_future(poll(k)) for k in kinds}
        pending, msgs = set(polls.values()), []
        while pending and not msgs:
            done, pending = await asyncio.wait(pending, timeout=timeout + 1, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for t in done:
                msgs += t.result()
        self.polls.update({k: t for k, t in polls.items() if not t.done()})
        for extra in msgs[n:]:  # over-fetched across kinds: hand them straight back
            await extra.nak()
        return [(m.subject.rsplit(".", 1)[-1], int(m.data), m) for m in msgs[:n]]

    def ack(self, msg: Any) -> None:
        self._soft(msg.ack())

    def nak(self, msg: Any, delay: float = 1.0) -> None:
        self._soft(msg.nak(delay=delay))

    # ---------- events ----------
    def event(self, **data: Any) -> None:
        """Fire-and-forget notification (job finished, rollout graded, ...) for live UIs."""
        if self.nc is not None and self.nc.is_connected:
            self._soft(self.nc.publish(self.events, json.dumps(data).encode()))

    def subscribe_events(self, callback: Callable[[dict], None]) -> Callable[[], None]:
        """Call `callback(event)` (on the bus thread) for every event; returns an unsubscribe function."""
        if not self.connected():
            return lambda: None

        async def handler(msg: Any) -> None:
            callback(json.loads(msg.data))

        sub = self._run(self.nc.subscribe(self.events, cb=handler))
        return lambda: self._soft(sub.unsubscribe())

    def _soft(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            self._run(coro)
        except Exception as e:
            log.debug("nats: %s", e)


_bus: Bus | None = None
_bus_lock = threading.Lock()


def get() -> Bus:
    global _bus
    with _bus_lock:
        if _bus is None:
            _bus = Bus()
        return _bus
