"""HTTP API + UI. Stateless: run as many replicas as you like behind a load balancer."""

import asyncio
import contextlib
import hmac
import json
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, bench, bus, config, db, envs, index, llm, metrics, pipeline, qa, queue


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    await run_in_threadpool(db.init)
    yield
    db.close()


def require_token(request: Request) -> None:
    """If FORGE_API_TOKEN is set, mutating routes need `Authorization: Bearer <token>`; reads stay open."""
    token = os.environ.get("FORGE_API_TOKEN")
    if not token or request.method in ("GET", "HEAD", "OPTIONS"):
        return
    given = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(given, f"Bearer {token}".encode()):
        raise HTTPException(401, "missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})


app = FastAPI(title="forge", lifespan=lifespan, dependencies=[Depends(require_token)])

# input bounds: keep one request from fanning out into unbounded work (forks) or memory (MinHash of a huge body)
MAX_FORKS = 8
MAX_K = 50
MAX_LIMIT = 500
MAX_TEXT = 200_000


@app.middleware("http")
async def record_latency(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", "unmatched")
    if path not in ("/metrics", "/api/events"):
        metrics.REQUEST_SECONDS.labels(request.method, path, response.status_code).observe(time.perf_counter() - start)
    return response


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, bool]:
    """Liveness: the process is up."""
    return {"ok": True}


@app.get("/readyz", include_in_schema=False)
def readyz() -> JSONResponse:
    """Readiness: Postgres reachable. NATS is reported but optional (workers fall back to Postgres)."""
    checks = {"postgres": False, "nats": False}
    with contextlib.suppress(Exception):
        checks["postgres"] = db.one("SELECT 1 AS ok")["ok"] == 1
    with contextlib.suppress(Exception):
        checks["nats"] = bus.get().connected()
    return JSONResponse(checks, status_code=200 if checks["postgres"] else 503)


@app.get("/metrics", include_in_schema=False)
def prometheus() -> Response:
    body, content_type = metrics.render()
    return Response(body, media_type=content_type)


@app.exception_handler(db.NotFound)
async def not_found(_: Request, exc: db.NotFound) -> PlainTextResponse:
    return PlainTextResponse(str(exc), status_code=404)


# The UI lives in web/ and is built to web/dist (`mise run web`).
WEB = Path(os.environ.get("FORGE_WEB", Path(__file__).resolve().parents[2] / "web" / "dist"))
if (WEB / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


def _j(row: db.Row, *keys: str) -> db.Row:
    for k in keys:
        if row.get(k):
            try:
                row[k] = json.loads(row[k])
            except Exception:
                pass
    return row


@app.get("/")
def ui() -> Response:
    if not (WEB / "index.html").exists():
        return PlainTextResponse("UI not built. Run `mise run web` (or `npm --prefix web run build`).", status_code=503)
    return FileResponse(WEB / "index.html")


@app.get("/api/overview")
def overview() -> dict[str, Any]:
    t = db.q("SELECT status, count(*) n FROM tasks GROUP BY status")
    r = db.q("SELECT kind, status, verdict, robust_pass, naive_pass, count(*) n FROM rollouts GROUP BY 1,2,3,4,5")
    return {
        "models": {"coder": config.CODER_MODEL, "embed": config.EMBED_MODEL, "ollama": llm.ping()},
        "tasks": {x["status"]: x["n"] for x in t},
        "rollouts": r,
        "jobs": queue.stats(),
        "chunks": db.one("SELECT count(*) n FROM chunks")["n"],
        "snapshots": db.one("SELECT count(*) n, avg(commit_ms) ms FROM snapshots"),
        "repos": db.q("SELECT * FROM repos ORDER BY name"),
    }


@app.post("/api/pipeline/start")
def start(repos: list[str] | None = Body(default=None)) -> dict[str, bool]:
    unknown = sorted(set(repos or []) - set(config.REPOS))
    if unknown:
        raise HTTPException(422, f"unknown repos: {unknown}")
    pipeline.start_pipeline(repos)
    return {"ok": True}


@app.get("/api/tasks")
def tasks(status: str | None = None) -> list[db.Row]:
    sql = (
        "SELECT id, repo, title, status, reject_reason, lines_changed, mutation_score, f2p, judge, "
        "(SELECT count(*) FROM rollouts r WHERE r.task_id=t.id) rollouts, "
        "(SELECT count(*) FROM rollouts r WHERE r.task_id=t.id AND r.robust_pass=1) solved FROM tasks t"
    )
    rows = db.q(sql + (" WHERE status=?" if status else "") + " ORDER BY created DESC", *([status] if status else []))
    for r in rows:
        r["f2p"] = len(json.loads(r["f2p"] or "[]"))
        judge = json.loads(r["judge"] or "{}")
        r["judge"] = judge.get("solvable") if isinstance(judge, dict) else None
    return rows


@app.get("/api/tasks/{tid:path}/contamination")
def task_contamination(tid: str) -> dict[str, Any]:
    from . import code

    t = db.q1("SELECT gold_patch FROM tasks WHERE id=?", tid)
    if not t:
        raise HTTPException(404)
    added = "\n".join(line for f in code.parse_patch(t["gold_patch"]).values() for line in f["added_text"])
    return index.contamination(added)


@app.get("/api/tasks/{tid:path}")
def task(tid: str) -> db.Row:
    t = db.q1("SELECT * FROM tasks WHERE id=?", tid)
    if not t:
        raise HTTPException(404)
    _j(t, "src_files", "test_files", "symbols", "f2p", "p2p", "judge", "mutants")
    t["rollouts"] = db.q(
        "SELECT id, parent_id, fork_step, kind, status, steps, stop_reason, naive_pass, robust_pass, verdict "
        "FROM rollouts WHERE task_id=? ORDER BY id",
        tid,
    )
    t["hacks"] = [
        _j(h, "flags")
        for h in db.q(
            "SELECT kind,is_hack,naive_pass,robust_pass,flagged,static_flagged,flags FROM hack_samples WHERE task_id=?",
            tid,
        )
    ]
    return t


@app.post("/api/tasks/{tid:path}/rollout")
def new_rollout(tid: str, kind: Literal["agent", "redteam"] = Body("agent", embed=True)) -> dict[str, Any]:
    return {"rollout": agent.create_rollout(tid, kind, priority=5)}


@app.get("/api/rollouts")
def rollouts(verdict: str | None = None) -> list[db.Row]:
    sql = (
        "SELECT id, task_id, parent_id, fork_step, kind, status, steps, stop_reason, naive_pass, robust_pass, verdict, flags, "
        "prompt_tokens, completion_tokens, created, finished FROM rollouts"
    )
    rows = db.q(
        sql + (" WHERE verdict=?" if verdict else "") + " ORDER BY id DESC LIMIT 500", *([verdict] if verdict else [])
    )
    for r in rows:
        r["flags"] = [f["rule"] for f in json.loads(r["flags"] or "[]") if f["severity"] != "info"]
    return rows


@app.get("/api/rollouts/{rid}")
def rollout(rid: int) -> db.Row:
    r = db.q1("SELECT * FROM rollouts WHERE id=?", rid)
    if not r:
        raise HTTPException(404)
    _j(r, "flags", "judge", "env_diff")
    r["steps_detail"] = [
        _j(s, "action")
        for s in db.q(
            "SELECT step, image, action, observation, commit_ms FROM snapshots WHERE rollout_id=? ORDER BY step", rid
        )
    ]
    # whole tree this rollout belongs to
    root = r
    while root["parent_id"] is not None:
        root = db.one("SELECT * FROM rollouts WHERE id=?", root["parent_id"])
    tree = db.q(
        "SELECT id, parent_id, fork_step, status, steps, robust_pass, verdict FROM rollouts WHERE task_id=? AND kind=?",
        r["task_id"],
        r["kind"],
    )
    r["tree_root"] = root["id"]
    r["tree"] = tree
    if r["parent_id"] is not None and r["fork_step"]:
        r["prefix"] = [
            _j(s, "action")
            for s in db.q(
                "SELECT step, action, observation FROM snapshots WHERE rollout_id=? AND step<=? ORDER BY step",
                r["parent_id"],
                r["fork_step"],
            )
        ]
    return r


@app.post("/api/rollouts/{rid}/fork")
def fork(
    rid: int, step: int = Body(..., ge=0, embed=True), n: int = Body(2, ge=1, le=MAX_FORKS, embed=True)
) -> dict[str, Any]:
    if step > (db.one("SELECT steps FROM rollouts WHERE id=?", rid)["steps"] or 0):
        raise HTTPException(422, "step is past the end of this rollout")
    return {"rollouts": agent.fork(rid, step, n)}


@app.get("/api/qa")
def qa_view() -> dict[str, Any]:
    flagged = db.q(
        "SELECT id, task_id, kind, verdict, flags, naive_pass, robust_pass FROM rollouts "
        "WHERE verdict IN ('hack','suspect') ORDER BY id DESC"
    )
    for f in flagged:
        f["flags"] = json.loads(f["flags"] or "[]")
    return {"corpus": qa.corpus_summary(), "flagged_rollouts": flagged}


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1, max_length=2000), k: int = Query(10, ge=1, le=MAX_K)
) -> dict[str, Any]:
    return await run_in_threadpool(index.search, q, k)


@app.post("/api/contamination")
async def contamination(code: str = Body(..., embed=True, max_length=MAX_TEXT)) -> dict[str, Any]:
    return await run_in_threadpool(index.contamination, code)


@app.get("/api/index")
async def index_stats() -> dict[str, Any]:
    return {
        "repos": await run_in_threadpool(index.stats),
        "near_duplicates": await run_in_threadpool(index.near_duplicates),
    }


@app.get("/api/envs")
def env_list() -> dict[str, Any]:
    return envs.list_envs()


@app.post("/api/envs")
async def env_create(snapshot: int | None = Body(None, embed=True)) -> dict[str, Any]:
    return await run_in_threadpool(envs.create, snapshot)


@app.post("/api/envs/{eid}/write")
async def env_write(eid: int, n: int = Body(10, ge=1, le=1000, embed=True)) -> Any:
    return await run_in_threadpool(envs.write, eid, n)


@app.get("/api/envs/{eid}/state")
async def env_state(eid: int) -> Any:
    return await run_in_threadpool(envs.state, eid)


@app.post("/api/envs/{eid}/snapshot")
async def env_snapshot(eid: int) -> dict[str, Any]:
    return await run_in_threadpool(envs.snapshot, eid)


@app.delete("/api/envs/{eid}")
async def env_destroy(eid: int) -> dict[str, bool]:
    await run_in_threadpool(envs.destroy, eid)
    return {"ok": True}


@app.get("/api/jobs")
def jobs(status: str | None = None, limit: int = Query(200, ge=1, le=MAX_LIMIT)) -> list[db.Row]:
    sql = "SELECT id, kind, payload, status, priority, attempts, lease_owner, error, created, updated FROM jobs"
    rows = db.q(
        sql + (" WHERE status=?" if status else "") + " ORDER BY updated DESC LIMIT ?",
        *([status] if status else []),
        limit,
    )
    for r in rows:
        r["error"] = (r["error"] or "").splitlines()[0][:200] if r["error"] else None
    return rows


@app.post("/api/jobs/{jid}/retry")
def retry(jid: int) -> dict[str, bool]:
    if not queue.retry(jid):
        raise HTTPException(409, "only dead or done jobs can be retried")
    return {"ok": True}


@app.get("/api/bench")
def bench_view() -> dict[str, Any]:
    b = db.get_bench()
    b["pipeline"] = bench.pipeline_stats()
    return b


WORKER_TTL = 15.0  # a worker that hasn't checked in for this long is considered gone


@app.get("/api/workers")
def workers() -> dict[str, Any]:
    rows = db.q("SELECT owner, concurrency, active FROM workers WHERE seen > ?", time.time() - WORKER_TTL)
    if not rows:
        return {"present": False}
    active = [job for r in rows for job in json.loads(r["active"] or "[]")]
    return {
        "present": True,
        "paused": db.get_control("paused") == "1",
        "count": len(rows),
        "concurrency": sum(r["concurrency"] for r in rows),
        "active": active,
    }


@app.post("/api/workers/{action}")
def worker_control(action: Literal["pause", "resume"]) -> dict[str, Any]:
    """Cluster-wide: every worker picks this up within a few seconds; running jobs finish."""
    db.set_control("paused", "1" if action == "pause" else "0")
    return workers()


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    """Server-sent events relayed from NATS, so the UI refreshes when something happens instead of polling."""
    loop = asyncio.get_running_loop()
    inbox: asyncio.Queue[dict] = asyncio.Queue(maxsize=1000)

    def put(event: dict) -> None:  # on the event loop
        if inbox.full():  # slow client: drop the oldest event (they're only refresh hints; the UI refetches)
            inbox.get_nowait()
        inbox.put_nowait(event)

    def deliver(event: dict) -> None:  # on the bus thread
        with contextlib.suppress(RuntimeError):  # loop already closed: the client is gone
            loop.call_soon_threadsafe(put, event)

    unsubscribe = await run_in_threadpool(bus.get().subscribe_events, deliver)

    async def stream() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(inbox.get(), 15)
                    yield f"data: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            # off the loop (it can block on NATS) and not awaited (we may be getting cancelled)
            loop.run_in_executor(None, unsubscribe)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/activity")
def activity(limit: int = Query(30, ge=1, le=MAX_LIMIT)) -> list[db.Row]:
    rows = db.q(
        "SELECT id, kind, payload, status, result, error, updated FROM jobs "
        "WHERE status IN ('done','dead') AND kind != 'chaos' ORDER BY updated DESC LIMIT ?",
        limit,
    )
    for r in rows:
        _j(r, "payload", "result")
    graded = [r["payload"].get("rollout") for r in rows if r["kind"] == "grade" and isinstance(r["payload"], dict)]
    current = {
        r["id"]: r
        for r in db.q(
            "SELECT id, robust_pass, verdict, flags FROM rollouts WHERE status='graded' AND id = ANY(?)",
            [i for i in graded if isinstance(i, int)],
        )
    }
    for r in rows:
        r["error"] = (r["error"] or "").splitlines()[0][:160] if r["error"] else None
        # a rollout can be re-graded later; show its current verdict, not the one at the time of the job
        cur = (
            current.get(r["payload"].get("rollout"))
            if r["kind"] == "grade" and isinstance(r["payload"], dict)
            else None
        )
        if cur:
            flags = [f["rule"] for f in json.loads(cur["flags"] or "[]") if f["severity"] != "info"]
            r["result"] = {"robust": bool(cur["robust_pass"]), "verdict": cur["verdict"], "flags": flags}
    return rows
