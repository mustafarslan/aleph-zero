"""Opening the index: one SQLite database per repository, shared by all its worktrees."""
from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

import apsw

from .. import gitio

SCHEMA_VERSION = 1
MIN_SQLITE = (3, 51, 3)  # earlier versions can corrupt a WAL database with two writers
_SCHEMA = Path(__file__).with_name("schema.sql")


class StoreError(RuntimeError):
    """The index cannot be opened as it is."""


def index_file(common_dir: Path) -> Path:
    return common_dir / "aleph-zero" / "index.db"


def index_path(cwd: Path) -> Path:
    """The index for the repository containing `cwd`; the same file from any of its worktrees."""
    return index_file(gitio.common_dir(cwd))


@contextmanager
def transaction(conn: apsw.Connection):
    """A short write transaction. IMMEDIATE takes the write lock up front, so a second
    writer waits for the busy timeout instead of failing halfway through."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _retry_while_busy(action, seconds: float = 10.0):
    """Two sessions can open a brand-new index at the same instant. Switching to WAL
    needs a moment of exclusive access and fails at once instead of waiting, so retry."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return action()
        except apsw.BusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def open_db(path: Path) -> apsw.Connection:
    version = tuple(int(part) for part in apsw.sqlite_lib_version().split("."))
    if version < MIN_SQLITE:
        raise StoreError(f"SQLite {apsw.sqlite_lib_version()} is older than {'.'.join(map(str, MIN_SQLITE))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = apsw.Connection(str(path))
    conn.set_busy_timeout(5000)
    _retry_while_busy(lambda: conn.pragma("journal_mode", "wal"))
    conn.pragma("synchronous", "normal")
    conn.pragma("cache_size", -32000)
    conn.pragma("foreign_keys", True)
    conn.pragma("temp_store", "memory")

    def create_schema_if_new() -> int:
        with transaction(conn):
            if conn.pragma("user_version") == 0:  # checked under the write lock
                conn.execute(_SCHEMA.read_text())
                conn.pragma("user_version", SCHEMA_VERSION)
        return conn.pragma("user_version")

    found = _retry_while_busy(create_schema_if_new)
    if found != SCHEMA_VERSION:
        raise StoreError(f"{path} has schema version {found}; this build reads version {SCHEMA_VERSION}")
    # The current view, per connection: which blobs and chunks a query may return.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS view_blobs(blob_oid TEXT PRIMARY KEY) WITHOUT ROWID")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS view_chunks(chunk_id INTEGER PRIMARY KEY)")
    # A query's kind or path filter narrows the view before candidates are ranked.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS scope_blobs(blob_oid TEXT PRIMARY KEY) WITHOUT ROWID")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS scope_chunks(chunk_id INTEGER PRIMARY KEY)")
    return conn
