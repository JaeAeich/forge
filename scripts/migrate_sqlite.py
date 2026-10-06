"""One-off: copy a pre-Postgres forge SQLite database (data/forge.db) into Postgres.

Usage: uv run python scripts/migrate_sqlite.py [path/to/forge.db]
"""

import sqlite3
import sys
from pathlib import Path

import numpy as np

from forge import code, db

TABLES = ["repos", "tasks", "rollouts", "snapshots", "hack_samples", "envs", "env_snapshots", "bench", "jobs", "chunks"]
SKIP_COLUMNS = {"chunks": {"emb"}}  # converted separately (bytes -> pgvector)


def main(path: str) -> None:
    src = sqlite3.connect(path)
    src.row_factory = sqlite3.Row
    db.init()
    for table in TABLES:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            continue
        cols = [c for c in rows[0].keys() if c not in SKIP_COLUMNS.get(table, set())]  # noqa: SIM118
        with db.tx() as c:
            c.execute(f"TRUNCATE {table} CASCADE")
            for r in rows:
                values = [r[k] for k in cols]
                if table == "chunks":
                    emb = np.frombuffer(r["emb"], dtype=np.float32)
                    c.execute(
                        f"INSERT INTO chunks({','.join(cols)}, emb) VALUES({','.join('?' * len(cols))}, ?)",
                        (*values, emb),
                    )
                    sig = np.frombuffer(r["minhash"], dtype=np.uint64)
                    for band, key in enumerate(code.band_keys(sig)):
                        c.execute(
                            "INSERT INTO chunk_bands(chunk_id, band, key) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                            (r["id"], band, key),
                        )
                else:
                    c.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", values)
            if "id" in cols and table not in ("tasks",):
                c.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT max(id) FROM {table}))")
        print(f"{table:14} {len(rows)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path("data/forge.db")))
