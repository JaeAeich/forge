"""Demo app for multi-node environments: every order is written to both Postgres and Redis.

The two stores agreeing after a restore is what proves a group snapshot was consistent.
"""

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
import redis


def connect() -> tuple[psycopg.Connection, redis.Redis]:
    for _ in range(120):
        try:
            pg = psycopg.connect("host=db user=postgres password=forge dbname=postgres", autocommit=True)
            r = redis.Redis(host="cache")
            r.ping()
            return pg, r
        except Exception:
            time.sleep(0.5)
    raise SystemExit("dependencies not ready")


pg, r = connect()
pg.execute("CREATE TABLE IF NOT EXISTS orders(id serial primary key, amount int, at timestamptz default now())")


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        n = int(self.path.rsplit("/", 1)[-1] or 1)
        for i in range(n):
            pg.execute("INSERT INTO orders(amount) VALUES(%s)", (i + 1,))
            r.incr("orders")
        self._send({"ok": n})

    def do_GET(self) -> None:
        row = pg.execute("SELECT count(*), coalesce(sum(amount), 0) FROM orders").fetchone() or (0, 0)
        self._send({"pg_orders": row[0], "pg_sum": row[1], "redis_counter": int(r.get("orders") or 0)})

    def log_message(self, format: str, *args: object) -> None:
        pass


ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
