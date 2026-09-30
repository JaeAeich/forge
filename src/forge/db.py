"""Postgres access: a connection pool, versioned migrations, and small query helpers.

Queries use `?` placeholders (translated to psycopg's `%s`) so SQL reads the same everywhere.
Every helper borrows a pooled connection for one statement; `tx()` holds one for a transaction.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from importlib import resources
from typing import Any, LiteralString, cast

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

Conn = psycopg.Connection[dict[str, Any]]
Row = dict[str, Any]

_PARAM = re.compile(r"\?")
_MIGRATION_LOCK = 4242  # pg advisory lock id: api and workers may start (and migrate) at the same time


class NotFound(LookupError):
    pass


def sql(text: str) -> LiteralString:
    """`?` placeholders -> `%s` (literal `%` escaped first).

    SQL text always comes from this codebase (values are always bound parameters), so the
    LiteralString cast that psycopg's type hints ask for is sound."""
    return cast(LiteralString, _PARAM.sub("%s", text.replace("%", "%%")))


def connect(url: str | None = None) -> Conn:
    """A standalone connection (migrations, benchmarks). Application code uses the pool."""
    c = cast(Conn, psycopg.connect(url or config.DATABASE_URL, autocommit=True, row_factory=dict_row))  # ty: ignore[invalid-argument-type]  (psycopg overload not resolved)
    register_vector(c)
    return c


# ---------- migrations ----------
def migrate(url: str | None = None) -> list[str]:
    """Apply pending `migrations/NNNN_*.sql` files in order, in one transaction. Idempotent."""
    files = sorted(
        (f for f in resources.files("forge.migrations").iterdir() if f.name.endswith(".sql")), key=lambda f: f.name
    )
    applied: list[str] = []
    with psycopg.connect(url or config.DATABASE_URL, autocommit=True) as c, c.transaction():
        c.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK,))
        c.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied DOUBLE PRECISION)")
        done = {r[0] for r in c.execute("SELECT version FROM schema_migrations").fetchall()}
        for f in files:
            version = f.name.removesuffix(".sql")
            if version in done:
                continue
            c.execute(cast(LiteralString, f.read_text()))
            c.execute("INSERT INTO schema_migrations VALUES (%s, %s)", (version, time.time()))
            applied.append(version)
    return applied


# ---------- pool ----------
_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> ConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                config.DATABASE_URL,
                min_size=1,
                max_size=config.DB_POOL_SIZE,
                kwargs={"autocommit": True, "row_factory": dict_row},
                configure=register_vector,
                check=ConnectionPool.check_connection,
                name="forge",
                open=True,
            )
        return _pool


def init() -> None:
    migrate()
    pool()


def close() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


class Tx:
    """A connection inside a transaction; keeps the `?` placeholder style."""

    def __init__(self, c: Conn):
        self.c = c

    def execute(self, text: str, args: Any = ()) -> psycopg.Cursor[Row]:
        return self.c.execute(sql(text), args)

    def one(self, text: str, args: Any = ()) -> Row:
        row = self.execute(text, args).fetchone()
        if row is None:
            raise NotFound(f"no row for: {text} {args}")
        return row


@contextmanager
def tx() -> Generator[Tx]:
    with pool().connection() as c, c.transaction():
        yield Tx(cast(Conn, c))


@contextmanager
def _borrow() -> Generator[Conn]:
    with pool().connection() as c:
        yield cast(Conn, c)  # the pool is configured with dict_row


def q(text: str, *args: Any) -> list[Row]:
    with _borrow() as c:
        return c.execute(sql(text), args).fetchall()


def q1(text: str, *args: Any) -> Row | None:
    with _borrow() as c:
        return c.execute(sql(text), args).fetchone()


def one(text: str, *args: Any) -> Row:
    """Exactly the row the caller expects to exist; raises NotFound otherwise."""
    row = q1(text, *args)
    if row is None:
        raise NotFound(f"no row for: {text} {args}")
    return row


def ex(text: str, *args: Any) -> int:
    """Run a statement; returns the number of affected rows."""
    with _borrow() as c:
        return c.execute(sql(text), args).rowcount


def _val(v: Any) -> Any:
    return json.dumps(v) if isinstance(v, (dict, list)) else v


def insert(table: str, **cols: Any) -> Any:
    keys = ",".join(cols)
    ph = ",".join("?" * len(cols))
    return one(f"INSERT INTO {table}({keys}) VALUES({ph}) RETURNING id", *map(_val, cols.values()))["id"]


def update(table: str, key: str, key_val: Any, **cols: Any) -> None:
    sets = ",".join(f"{k}=?" for k in cols)
    ex(f"UPDATE {table} SET {sets} WHERE {key}=?", *map(_val, cols.values()), key_val)


# ---------- small key/value tables ----------
def get_control(key: str, default: str = "") -> str:
    row = q1("SELECT value FROM control WHERE key=?", key)
    return row["value"] if row else default


def set_control(key: str, value: str) -> None:
    ex("INSERT INTO control(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)


def put_bench(name: str, data: dict) -> None:
    ex(
        "INSERT INTO bench(name,data,created) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET data=excluded.data, created=excluded.created",
        name,
        json.dumps(data),
        time.time(),
    )


def get_bench() -> dict[str, dict]:
    return {r["name"]: {**json.loads(r["data"]), "_at": r["created"]} for r in q("SELECT * FROM bench")}
