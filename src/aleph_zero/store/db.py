"""Opening the index: one SQLite database per repository, shared by all its worktrees."""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
import threading
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import apsw

from .. import gitio

SCHEMA_VERSION = 11
BUSY_MS = 5000       # how long a statement waits for another session's write
MIN_SQLITE = (3, 51, 3)  # earlier versions can corrupt a WAL database with two writers
_SCHEMA = Path(__file__).with_name("schema.sql")


class StoreError(RuntimeError):
    """The index cannot be opened as it is."""


# Raised by SQLite when the file is not a database any more: overwritten, cut short, or with
# damaged pages. The index is a cache, so the answer is to set the file aside and build again.
DAMAGED = (apsw.CorruptError, apsw.NotADBError)
_NETWORK_FILESYSTEMS = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "afs", "ceph", "glusterfs", "lustre",
                        "ncpfs", "fuse.sshfs"}
_decided: dict[Path, tuple[Path, str]] = {}
_deciding = threading.Lock()
_notes: dict[Path, str] = {}


def is_local(path: Path) -> bool:
    """False on a network filesystem. SQLite's write-ahead log shares memory between
    processes through a mapped file, which network filesystems do not give reliably."""
    try:
        if sys.platform == "darwin":
            class Statfs(ctypes.Structure):
                _fields_ = [("f_bsize", ctypes.c_uint32), ("f_iosize", ctypes.c_int32),
                            ("f_blocks", ctypes.c_uint64), ("f_bfree", ctypes.c_uint64),
                            ("f_bavail", ctypes.c_uint64), ("f_files", ctypes.c_uint64),
                            ("f_ffree", ctypes.c_uint64), ("f_fsid", ctypes.c_int32 * 2),
                            ("f_owner", ctypes.c_uint32), ("f_type", ctypes.c_uint32),
                            ("f_flags", ctypes.c_uint32), ("f_fssubtype", ctypes.c_uint32),
                            ("f_fstypename", ctypes.c_char * 16), ("f_mntonname", ctypes.c_char * 1024),
                            ("f_mntfromname", ctypes.c_char * 1024), ("f_flags_ext", ctypes.c_uint32),
                            ("f_reserved", ctypes.c_uint32 * 7)]

            libc = ctypes.CDLL(None, use_errno=True)
            call = getattr(libc, "statfs$INODE64", None) or libc.statfs
            found = Statfs()
            if call(os.fsencode(path), ctypes.byref(found)) != 0:
                return True
            return bool(found.f_flags & 0x1000)  # MNT_LOCAL
        best, kind = "", ""
        target = os.path.realpath(path)
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as mounts:
            for line in mounts:
                left, _, right = line.partition(" - ")
                mount_point = left.split()[4]
                if (target == mount_point or target.startswith(mount_point.rstrip("/") + "/")) \
                        and len(mount_point) >= len(best):
                    best, kind = mount_point, right.split()[0]
        return kind not in _NETWORK_FILESYSTEMS
    except (OSError, AttributeError, IndexError):
        return True  # no way to tell: behave as before


def _writable(directory: Path) -> bool:
    """Whether files can be created in `directory`, creating it if its parent allows."""
    # A name of this thread's own: with one name a process, two threads probing at once removed each other's file,
    # and the second took the missing file for a directory that cannot be written.
    probe = directory / f".probe-{os.getpid()}-{threading.get_ident()}"
    try:
        directory.mkdir(exist_ok=True)
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cache_dir(common_dir: Path) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    key = hashlib.sha256(str(common_dir).encode()).hexdigest()[:16]
    return base / "aleph-zero" / "index" / key


def cache_dir_for(directory: Path) -> Path:
    """Where the state of a directory that is not a repository is kept: always in the cache, never beside its
    files. Used as that directory's `common_dir`, so nothing that asks where state lives can answer otherwise."""
    cache = _cache_dir(directory)
    cache.mkdir(parents=True, exist_ok=True)
    label = cache / "repository"
    if not label.exists():
        label.write_text(f"{directory}\n")
    # The convention by which a tool marks what it generates: backups skip it, and so does Aleph's own walk of a
    # plain directory, should the cache lie inside one.
    tag = cache.parents[1] / "CACHEDIR.TAG"
    if not tag.exists():
        tag.write_text("Signature: 8a477f597d28d172789f06886806bc55\n# Aleph Zero's cache: indexes of code. Deleting it loses nothing but time.\n")
    return cache


def _decide(common_dir: Path) -> tuple[Path, str]:
    beside = common_dir / "aleph-zero"
    if not is_local(common_dir):
        reason = "the repository is on a network filesystem, where the index cannot be shared safely"
    elif _writable(beside):
        return beside, ""
    else:
        reason = f"{common_dir} cannot be written"
    cache = _cache_dir(common_dir)
    cache.mkdir(parents=True, exist_ok=True)
    label = cache / "repository"
    if not label.exists():
        label.write_text(f"{common_dir}\n")  # so a person can tell whose state this is
    return cache, reason


def state_dir(common_dir: Path) -> Path:
    """Where everything Aleph keeps for a repository lives: beside git's own data, shared by
    all its worktrees, or in the user's cache directory when that is not possible. Decided
    once per process: every session of one user comes to the same answer, since it depends
    only on the repository's directory."""
    found = _decided.get(common_dir)
    if found is None:
        with _deciding:     # once, also when two threads of a session ask first at the same moment
            found = _decided.get(common_dir)
            if found is None:
                found = _decided[common_dir] = _decide(common_dir)
    return found[0]


def state_reason(common_dir: Path) -> str:
    """Why the state is not beside git's data; empty when it is."""
    state_dir(common_dir)
    return _decided[common_dir][1]


def forget_state_dirs() -> None:
    """Decide again on next use (tests, and a session that outlives a permission change)."""
    _decided.clear()
    _notes.clear()


def index_file(common_dir: Path) -> Path:
    return state_dir(common_dir) / "index.db"


def index_note(common_dir: Path) -> str:
    """What a person should know about this index: why it is where it is, and whether it
    was rebuilt after damage in this session."""
    parts = [state_reason(common_dir), _notes.get(index_file(common_dir), "")]
    return "; ".join(part for part in parts if part)


def inode(path: Path) -> int:
    try:
        return path.stat().st_ino
    except OSError:
        return 0


def set_aside(path: Path, opened: int) -> bool:
    """Move a damaged index file out of the way, keeping it for a look, together with the
    vector files whose rows it numbers. `opened` is the inode this session had open: if
    the file at `path` is a different one by now, another session has already rebuilt it
    and nothing is moved. Returns whether it moved. One session at a time: without the
    lock, a second session could look before the first one's rename and move the new index."""
    with open(path.parent / "set-aside.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if opened and inode(path) != opened:
                return False
            stamp = f"damaged-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
            target = path.with_name(f"{path.name}.{stamp}")
            try:
                os.replace(path, target)
            except FileNotFoundError:
                return False
            # The log files belong to the damaged file and would be replayed into a new one. The
            # vector files hold rows by this database's numbering: a new database starts from row 0.
            for extra in (Path(str(path) + "-wal"), Path(str(path) + "-shm"), *path.parent.glob("vectors.*.f16")):
                try:
                    os.replace(extra, extra.with_name(f"{extra.name}.{stamp}" if extra.suffix == ".f16"
                                                      else f"{target.name}{extra.name[len(path.name):]}"))
                except FileNotFoundError:
                    pass
            _notes[path] = (f"rebuilt on {time.strftime('%Y-%m-%d %H:%M')}: the index file was damaged and is kept "
                            f"as {target.name}")
            return True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


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


def _upgrade_4(conn: apsw.Connection) -> None:
    """Schema 4 to 5: a write counter for the vector file, and counters all sessions share."""
    conn.execute("ALTER TABLE vector_files ADD COLUMN writes INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL) WITHOUT ROWID")


def _upgrade_5(conn: apsw.Connection) -> None:
    """Schema 5 to 6: the free list of the vector file, worked out once from the rows no
    chunk owns, with the index and the trigger that keep it true from here on."""
    conn.execute(_SCHEMA.read_text())  # every statement is IF NOT EXISTS: this adds what is new
    for kind, next_row in conn.execute("SELECT kind, next_row FROM vector_files").fetchall():
        conn.execute(
            "WITH RECURSIVE n(i) AS (SELECT 0 WHERE ? > 0 UNION ALL SELECT i + 1 FROM n WHERE i + 1 < ?) "
            "INSERT OR IGNORE INTO vector_free(kind, row) SELECT ?, i FROM n "
            "WHERE i NOT IN (SELECT row FROM chunk_vectors WHERE kind = ?)", (next_row, next_row, kind, kind))


def _upgrade_6(conn: apsw.Connection) -> None:
    """Schema 6 to 7: the table the code graph keeps its per-file facts in. It starts empty and
    is filled when a graph question is first asked; nothing indexed or embedded is touched."""
    conn.execute(_SCHEMA.read_text())


def _upgrade_7(conn: apsw.Connection) -> None:
    """Schema 7 to 8: the tables imported compiler indexes are kept in. They start empty."""
    conn.execute(_SCHEMA.read_text())


def _upgrade_8(conn: apsw.Connection) -> None:
    """Schema 8 to 9: the table architectural facts are kept in. It starts empty."""
    conn.execute(_SCHEMA.read_text())


def _upgrade_9(conn: apsw.Connection) -> None:
    """Schema 9 to 10: the table the vectors of whole decision records are kept in. It starts empty."""
    conn.execute(_SCHEMA.read_text())


def _upgrade_10(conn: apsw.Connection) -> None:
    """Schema 10 to 11: symbols defined inside functions (`nested`), and per blob which version wrote its symbols.
    Every blob starts at 0 and has its symbols read again by a long sync; its chunks and vectors are not touched."""
    for table, column in (("symbols", "nested"), ("blobs", "symbols_version")):
        if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")


# Versions that are brought up to date where they are, one step at a time. Anything older
# is a cache of a different shape and is rebuilt; these hold vectors that took GPU time.
_UPGRADES = {4: _upgrade_4, 5: _upgrade_5, 6: _upgrade_6, 7: _upgrade_7, 8: _upgrade_8, 9: _upgrade_9, 10: _upgrade_10}


def upgradable() -> frozenset[int]:
    """Schema versions this build brings up to date in place."""
    return frozenset(_UPGRADES)


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
    """Open the index, creating it if needed. A file that is not a database any more is set
    aside and a new index started."""
    before = inode(path)
    try:
        return _open(path)
    except DAMAGED:
        set_aside(path, before)  # not moved if another session has replaced it meanwhile
        return _open(path)


def _open(path: Path) -> apsw.Connection:
    version = tuple(int(part) for part in apsw.sqlite_lib_version().split("."))
    if version < MIN_SQLITE:
        raise StoreError(f"SQLite {apsw.sqlite_lib_version()} is older than {'.'.join(map(str, MIN_SQLITE))}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = apsw.Connection(str(path))
    conn.set_busy_timeout(BUSY_MS)
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
            while found in _UPGRADES:
                _UPGRADES[found](conn)
                found += 1
                conn.pragma("user_version", found)
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
