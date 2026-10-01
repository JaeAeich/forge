"""Shared fixtures. Database tests run against a throwaway Postgres database (skipped if none is reachable)."""

import pytest


class FakeBus:
    """Stands in for NATS: records announcements instead of sending them."""

    def __init__(self):
        self.published: list[tuple[str, int]] = []

    def connected(self) -> bool:
        return True

    def publish_job(self, kind: str, job_id: int, msg_id: str | None = None) -> bool:
        self.published.append((kind, job_id))
        return True

    def event(self, **_):
        pass


@pytest.fixture(scope="session")
def pg_url():
    import psycopg

    from forge import bench, config

    try:
        psycopg.connect(config.DATABASE_URL, connect_timeout=2).close()
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable (run `mise run infra`)")
    with bench.scratch_db("forge_test") as url:
        yield url


@pytest.fixture()
def pg(pg_url, monkeypatch):
    """Fresh tables for every test, on the session's scratch database."""
    from forge import config, db

    monkeypatch.setattr(config, "DATABASE_URL", pg_url)
    db.close()
    db.init()
    tables = [r["tablename"] for r in db.q("SELECT tablename FROM pg_tables WHERE schemaname='public'")]
    db.ex(f"TRUNCATE {', '.join(t for t in tables if t != 'schema_migrations')} RESTART IDENTITY CASCADE")
    yield db
    db.close()


@pytest.fixture()
def fake_bus(monkeypatch):
    from forge import bus

    fake = FakeBus()
    monkeypatch.setattr(bus, "get", lambda: fake)
    return fake
