"""Prometheus metrics. Pipeline gauges are read from Postgres at scrape time, so every API replica
reports the same cluster-wide numbers; request latency is per replica."""

import time
from collections.abc import Iterator

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from . import db

WORKER_TTL = 15.0

REQUEST_SECONDS = Histogram(
    "forge_http_request_seconds",
    "API request latency",
    ["method", "route", "status"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


class PipelineCollector(Collector):
    def describe(self) -> list[GaugeMetricFamily]:
        # without this, register() calls collect() to learn the metric names -- querying Postgres at import time
        return []

    def collect(self) -> Iterator[GaugeMetricFamily]:
        jobs = GaugeMetricFamily("forge_jobs", "Jobs by kind and status", labels=["kind", "status"])
        for r in db.q("SELECT kind, status, count(*) AS n FROM jobs GROUP BY 1, 2"):
            jobs.add_metric([r["kind"], r["status"]], r["n"])
        yield jobs

        tasks = GaugeMetricFamily("forge_tasks", "Tasks by status", labels=["status"])
        for r in db.q("SELECT status, count(*) AS n FROM tasks GROUP BY 1"):
            tasks.add_metric([r["status"]], r["n"])
        yield tasks

        rollouts = GaugeMetricFamily("forge_rollouts", "Graded rollouts", labels=["kind", "verdict", "solved"])
        for r in db.q(
            "SELECT kind, coalesce(verdict, 'none') AS verdict, coalesce(robust_pass, 0) AS solved, count(*) AS n"
            " FROM rollouts WHERE status='graded' GROUP BY 1, 2, 3"
        ):
            rollouts.add_metric([r["kind"], r["verdict"], str(bool(r["solved"])).lower()], r["n"])
        yield rollouts

        w = db.one(
            "SELECT count(*) AS n, coalesce(sum(concurrency), 0) AS slots FROM workers WHERE seen > ?",
            time.time() - WORKER_TTL,
        )
        yield GaugeMetricFamily("forge_workers", "Live worker processes", value=w["n"])
        yield GaugeMetricFamily("forge_worker_slots", "Total job slots across live workers", value=w["slots"])

        lag = db.one(
            "SELECT coalesce(max(? - created), 0) AS s FROM jobs WHERE status='queued' AND run_after <= ?",
            time.time(),
            time.time(),
        )
        yield GaugeMetricFamily("forge_queue_oldest_seconds", "Age of the oldest runnable queued job", value=lag["s"])


REGISTRY.register(PipelineCollector())


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
