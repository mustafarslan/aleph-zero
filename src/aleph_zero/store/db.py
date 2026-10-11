"""Opening the index: one SQLite database per repository, shared by all its worktrees."""
from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path

import apsw

from .. import gitio

SCHEMA_VERSION = 3
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


def _drop_everything(conn: apsw.Connection) -> None:
    """Remove every table. Virtual tables go first: they own their shadow tables."""
    for virtual in (True, False):
        names = [row[0] for row in conn.execute(
            "SELECT name FROM main.sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "AND (sql LIKE 'CREATE VIRTUAL%') = ?", (virtual,))]
        for name in names:
            conn.execute(f'DROP TABLE IF EXISTS main."{name}"')


def open_db(path: Path) -> apsw.Connection:
    version = tuple(int(part) for part in apsw.sqlite_lib_version().split("."))
    if version < MIN_SQLITE:
        raise StoreError(f"SQLite {apsw.sqlite_lib_version()} is older than {'.'.join(map(str, MIN_SQLITE))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = apsw.Connection(str(path))
    conn.set_busy_timeout(5000)
    _retry_while_busy(lambda: conn.pragma("journal_mode", "wal"))
    conn.pragma("synchronous", "normal")
    # 8 MB of page cache per session. 32 MB indexed Django no faster (10.1 s against 10.2 s),
    # answered queries no faster, and left 28 MB more private memory behind (M1c measurement).
    conn.pragma("cache_size", -8000)
    conn.pragma("foreign_keys", True)
    conn.pragma("temp_store", "memory")

    def create_schema() -> int:
        with transaction(conn):
            found = conn.pragma("user_version")  # checked under the write lock
            if 0 < found < SCHEMA_VERSION:
                _drop_everything(conn)  # the index is a cache: an older one is rebuilt
                found = 0
            if found == 0:
                conn.execute(_SCHEMA.read_text())
                conn.pragma("user_version", SCHEMA_VERSION)
        return conn.pragma("user_version")

    found = _retry_while_busy(create_schema)
    if found != SCHEMA_VERSION:
        raise StoreError(f"{path} has schema version {found}; this build reads version {SCHEMA_VERSION}")
    # The current view, per connection: which blobs and chunks a query may return.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS view_blobs("
                 "blob_oid TEXT NOT NULL, family TEXT NOT NULL, PRIMARY KEY (blob_oid, family)) WITHOUT ROWID")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS view_chunks(chunk_id INTEGER PRIMARY KEY)")
    # How many chunks hold each term: lets a long query keep its rarest words (lexical.select_terms).
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.chunk_vocab USING fts5vocab(main, chunk_fts, row)")
    # A query's kind or path filter narrows the view before candidates are ranked.
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS scope_blobs("
                 "blob_oid TEXT NOT NULL, family TEXT NOT NULL, PRIMARY KEY (blob_oid, family)) WITHOUT ROWID")
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS scope_chunks(chunk_id INTEGER PRIMARY KEY)")
    return conn
