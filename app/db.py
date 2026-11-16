"""SQLite persistence layer.

The database stores metadata plus the raw, immutable bytes of every accepted
revision (XBRL instances, XSD schemas, calculation linkbases) and every report,
so restarting the container loses nothing.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS packages (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    latest_rev  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS package_revisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id    TEXT NOT NULL REFERENCES packages(id),
    revision       INTEGER NOT NULL,
    xsd_raw        BLOB NOT NULL,
    calc_raw       BLOB NOT NULL,
    target_ns      TEXT NOT NULL,
    prefix         TEXT NOT NULL,
    concepts_json  TEXT NOT NULL,
    equations_json TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(package_id, revision)
);

CREATE TABLE IF NOT EXISTS instances (
    id          TEXT PRIMARY KEY,
    package_id  TEXT NOT NULL REFERENCES packages(id),
    latest_rev  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_revisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id       TEXT NOT NULL REFERENCES instances(id),
    revision          INTEGER NOT NULL,
    instance_raw      BLOB NOT NULL,
    entity            TEXT NOT NULL,
    period            TEXT NOT NULL,
    currency          TEXT NOT NULL,
    facts_json        TEXT NOT NULL,
    -- Package revision actually used to parse these facts.  NULL on rows that
    -- predate this column: their basis is unknown and must not be guessed.
    package_revision  INTEGER,
    created_at        TEXT NOT NULL,
    UNIQUE(instance_id, revision)
);

CREATE TABLE IF NOT EXISTS reports (
    id                        TEXT PRIMARY KEY,
    instance_id               TEXT NOT NULL,
    package_id                TEXT NOT NULL,
    instance_revision         INTEGER NOT NULL,
    package_revision          INTEGER NOT NULL,
    instance_package_revision INTEGER,
    status                    TEXT NOT NULL,            -- feasible | infeasible | pending
    is_correction             INTEGER NOT NULL,         -- 1 when an earlier report existed
    superseded                INTEGER NOT NULL DEFAULT 0,
    late                      INTEGER NOT NULL DEFAULT 0,
    witness_json              TEXT,
    conflicts_json            TEXT,
    missing_json              TEXT,
    detail_json               TEXT NOT NULL,
    instance_snapshot         BLOB NOT NULL,
    xsd_snapshot              BLOB NOT NULL,
    calc_snapshot             BLOB NOT NULL,
    created_at                TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_instance ON reports(instance_id, created_at);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Add a column to a pre-existing database (no backfill, no guessing)."""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "instance_revisions", "package_revision", "INTEGER")
    _ensure_column(conn, "reports", "instance_package_revision", "INTEGER")


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            config.ensure_dirs()
            Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(config.DB_PATH), check_same_thread=False, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(SCHEMA)
            _migrate(conn)
            _conn = conn
        return _conn


@contextmanager
def transaction():
    """Serialize a read-check-write unit and commit it atomically.

    Each transaction uses its own connection so concurrent readers on the
    shared connection never observe uncommitted rows (SQLite/WAL).  A
    process-wide lock plus ``BEGIN IMMEDIATE`` serializes writers, so a request
    that loses the race re-reads the committed newest revision and raises
    ``StaleRevision`` instead of appending a second revision derived from the
    same base.
    """
    get_conn()  # ensure directories/schema exist before opening the writer
    with _lock:
        path = str(config.DB_PATH)
        if path == ":memory:":  # pragma: no cover - tests and runtime use files
            conn = get_conn()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
            return

        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()


def init_db() -> None:
    get_conn()


def reset_conn() -> None:
    """Drop the cached connection (used by tests that swap the DB path)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
