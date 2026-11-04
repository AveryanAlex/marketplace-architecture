"""Versioned SQL migrations with checksums and a PostgreSQL advisory lock."""

import hashlib
import logging
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


def make_pool(url: str) -> ConnectionPool:
    pool = ConnectionPool(
        url,
        open=True,
        min_size=1,
        max_size=16,
        timeout=5,
        kwargs={"row_factory": dict_row, "connect_timeout": 5},
    )
    pool.wait(timeout=30)
    return pool


def migrate(pool: ConnectionPool, service: str) -> None:
    directory = Path(__file__).resolve().parents[2] / "migrations" / service
    with pool.connection() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(7419021)")
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        for path in sorted(directory.glob("*.sql")):
            sql = path.read_text()
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version = %s", (path.name,)
            ).fetchone()
            if row:
                if row["checksum"] != checksum:
                    raise RuntimeError(f"Migration changed after application: {path.name}")
                continue
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
            log.info("migration applied service=%s version=%s", service, path.name)
