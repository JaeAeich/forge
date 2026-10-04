"""forge command line. `forge api` and `forge worker` are the deployable pieces; `forge up` runs both."""

from __future__ import annotations

import argparse
import json
import signal
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .worker import Worker


def _run_until_signalled(worker: Worker) -> None:
    """Run a worker in the foreground; SIGTERM/SIGINT trigger a graceful drain (see Worker.shutdown)."""
    from . import config

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    worker.start()
    stop.wait()
    worker.shutdown(config.settings.shutdown_grace_seconds)


def _serve(host: str, port: int) -> None:
    import uvicorn

    from . import pipeline

    threading.Thread(target=pipeline.scheduler, args=(threading.Event(),), daemon=True, name="scheduler").start()
    uvicorn.run("forge.server:app", host=host, port=port, log_level="warning")


def _worker(concurrency: int, kinds: list[str] | None = None, lease: float = 60.0) -> Worker:
    from . import config, pipeline, sandbox
    from .worker import Worker

    sandbox.ensure_base()
    return Worker(kinds or pipeline.PIPELINE_KINDS, concurrency or config.WORKERS, lease, pipeline.KIND_LIMITS)


def cmd_up(a: argparse.Namespace) -> None:
    """Single machine: API + workers in one process. Safe to clean up orphans since we own this Docker host."""
    from . import db, envs, pipeline, sandbox

    sandbox.gc()
    db.ex("UPDATE envs SET status='destroyed' WHERE status!='destroyed'")
    threading.Thread(target=envs.ensure_images, daemon=True).start()
    _worker(a.workers).start()
    if a.start:
        pipeline.start_pipeline()
    _serve(a.host, a.port)


def cmd_api(a: argparse.Namespace) -> None:
    _serve(a.host, a.port)


def cmd_worker(a: argparse.Namespace) -> None:
    kinds = a.kinds.split(",") if a.kinds else None
    if kinds in (["chaos"], ["noop"]):  # benchmark workers need no sandboxes
        from . import pipeline  # noqa: F401  (registers the chaos handler)
        from .worker import Worker

        _run_until_signalled(Worker(kinds, a.concurrency, a.lease))
    else:
        _run_until_signalled(_worker(a.concurrency, kinds, a.lease))


def cmd_start(a: argparse.Namespace) -> None:
    from . import pipeline

    pipeline.start_pipeline(a.repos or None)


def cmd_bench(a: argparse.Namespace) -> None:
    from . import bench, db, envs, sandbox

    def save(name: str, result: dict) -> None:
        db.put_bench(name, result)
        print(json.dumps(result, indent=1))

    if a.which in ("chaos", "all"):
        save("chaos_tx", bench.chaos(a.n or 2000, mode="tx"))
        save("chaos_naive", bench.chaos(a.n or 2000, mode="naive"))
    if a.which in ("throughput", "all"):
        save("throughput", bench.throughput(a.n or 20_000))
    if a.which in ("agents", "all"):
        save("agents_sim", bench.agents_sim(a.n or 100_000))
    if a.which in ("sandboxes", "all"):
        sandbox.ensure_base()
        save("sandboxes", bench.sandboxes(a.n or 40))
    if a.which in ("envs", "all"):
        print(json.dumps(envs.demo(), indent=1))


def cmd_report(_: argparse.Namespace) -> None:
    from . import bench

    md = bench.report_md()
    with open("BENCHMARKS.md", "w") as f:
        f.write(md)
    print(md)


def cmd_status(_: argparse.Namespace) -> None:
    from . import bench, queue

    print(json.dumps({"jobs": queue.stats(), **bench.pipeline_stats()}, indent=1))


def cmd_gc(a: argparse.Namespace) -> None:
    from . import sandbox

    containers, images = sandbox.gc(a.images)
    print(f"removed containers={containers} images={images}")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser("forge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def web(p: argparse.ArgumentParser) -> None:
        p.add_argument("--host", default="127.0.0.1")
        p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("up", help="API + UI + workers in one process (local use)")
    web(p)
    p.add_argument("--workers", type=int, default=0, help="worker threads (default FORGE_WORKERS)")
    p.add_argument("--start", action="store_true", help="kick off the pipeline for all repos")
    p.set_defaults(fn=cmd_up)

    p = sub.add_parser("api", help="API + UI + scheduler, no workers")
    web(p)
    p.set_defaults(fn=cmd_api)

    p = sub.add_parser("worker", help="a worker process; run as many as you like")
    p.add_argument("--kinds", default=None, help="comma-separated job kinds (default: all pipeline kinds)")
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--lease", type=float, default=60)
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("start", help="queue mining for repos")
    p.add_argument("repos", nargs="*")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("bench", help="run benchmarks")
    p.add_argument("which", choices=["chaos", "throughput", "agents", "sandboxes", "envs", "all"])
    p.add_argument("--n", type=int, default=None)
    p.set_defaults(fn=cmd_bench)

    sub.add_parser("report", help="write BENCHMARKS.md").set_defaults(fn=cmd_report)
    sub.add_parser("status", help="print queue and pipeline stats").set_defaults(fn=cmd_status)
    p = sub.add_parser("gc", help="remove forge containers (and snapshot images)")
    p.add_argument("--images", action="store_true")
    p.set_defaults(fn=cmd_gc)
    return ap


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    from . import db, logs

    logs.setup()

    db.init()
    args.fn(args)


if __name__ == "__main__":
    main()
