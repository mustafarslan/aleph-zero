"""Long-term memory: what was asked, answered and noted about a repository in earlier sessions.

The catalogue is a small database of its own, `memory/memories.db` in the repository's
state directory. Not the index: the index is a cache of what git holds and is thrown away
and rebuilt when it is damaged or its shape changes, and a memory cannot be rebuilt from
anything. The catalogue is the authority for what exists, what it cites and what was
forgotten. Beside it sits a store of vectors for recall by
meaning: Aeon's Trace store when Aeon is installed (ADR-006), nothing otherwise, in which
case memories are recalled by their words and by recency. Nothing here fails because the
vector store is missing, broken or out of step: the catalogue alone always answers.

A memory cites code by path and blob id. It is stale when that blob is no longer at that
path in the asking session's view: the one thing a code index can say about a memory that
a memory store alone cannot.
"""
from __future__ import annotations

import fcntl
import gc
import importlib.util
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import apsw

from . import lexical
from .store import transaction
from .store.db import _retry_while_busy
from .view import View

KINDS = ("chat", "note")
OVER_ASK = 20        # Aeon's search has no filter: ask for this many times what is wanted
WORD_POOL = 2000     # the newest memories looked at for a match by words
_K = 60              # reciprocal rank fusion, as in search


_CATALOGUE = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,          -- 'chat' (a side-chat question and answer), 'note' (inserted into the conversation)
    session    TEXT NOT NULL,
    created_at INTEGER NOT NULL,       -- unix seconds
    text       TEXT NOT NULL,          -- emptied when the memory is forgotten
    terms      TEXT NOT NULL DEFAULT '',   -- its words as search reads them, so recall does not tokenise every text again
    aeon_id    INTEGER,           -- the store's own id of its event, when it gave one
    deleted    INTEGER NOT NULL DEFAULT 0,
    embedded   INTEGER NOT NULL DEFAULT 0  -- whether the vector store holds a vector for it
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS memories_by_time ON memories(deleted, created_at DESC);
-- The code a memory speaks of: path and content at the time.
CREATE TABLE IF NOT EXISTS memory_cites (
    memory_id  TEXT NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    blob_oid   TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    PRIMARY KEY (memory_id, path, start_line)
) WITHOUT ROWID;
"""


def catalogue_file(state: Path) -> Path:
    return state / "memory" / "memories.db"


def open_catalogue(state: Path, create: bool = True) -> apsw.Connection | None:
    """The catalogue of the repository whose state directory is `state`. With `create`
    false, None when no memory was ever kept there (reading must not leave a directory
    behind). Unlike the index, this file is never set aside or rebuilt: what it holds
    exists nowhere else."""
    path = catalogue_file(state)
    if not path.exists():
        if not create:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = apsw.Connection(str(path))
    conn.set_busy_timeout(5000)
    _retry_while_busy(lambda: conn.pragma("journal_mode", "wal"))
    conn.pragma("synchronous", "normal")
    conn.pragma("foreign_keys", True)
    conn.pragma("secure_delete", True)  # a forgotten text is overwritten, not just unlinked

    def create_schema() -> None:
        with transaction(conn):
            conn.execute(_CATALOGUE)

    _retry_while_busy(create_schema)

    def add_store_ids() -> None:     # a catalogue made before the store's ids were kept
        with transaction(conn):
            if "aeon_id" not in [row[1] for row in conn.execute("PRAGMA table_info(memories)")]:
                conn.execute("ALTER TABLE memories ADD COLUMN aeon_id INTEGER")

    _retry_while_busy(add_store_ids)
    return conn


class VectorStore(Protocol):
    """What recall by meaning needs from a store of vectors."""

    def append(self, memory_id: str, session: str, text: str, vector, when: int) -> None: ...
    def nearest(self, vector, limit: int) -> list[str]: ...
    def ids(self) -> set[str]: ...


@dataclass(frozen=True)
class Cite:
    path: str
    blob_oid: str
    start_line: int
    end_line: int
    stale: bool = False


@dataclass(frozen=True)
class Recalled:
    memory_id: str
    kind: str
    session: str
    created_at: int
    text: str
    cites: tuple[Cite, ...] = field(default_factory=tuple)
    stale: bool = False     # a file it cites has changed since, or is gone


class Memory:
    def __init__(self, conn: apsw.Connection, store: VectorStore | None) -> None:
        self.conn = conn
        self.store = store

    def remember(self, kind: str, text: str, session: str, vector=None, cites: list[Cite] | None = None,
                 now: int | None = None) -> str:
        """Keep one memory. The catalogue is written first and is enough by itself; the
        vector goes to the store if there is one and it works. Returns the memory's id."""
        if kind not in KINDS:
            raise ValueError(f"a memory is one of {KINDS}, not {kind!r}")
        text = text.strip()
        if not text:
            raise ValueError("an empty memory")
        memory_id = secrets.token_hex(12)
        when = int(time.time()) if now is None else int(now)
        terms = " ".join(sorted(set(lexical.query_terms(text))))
        with transaction(self.conn):
            self.conn.execute("INSERT INTO memories(memory_id, kind, session, created_at, text, terms) VALUES (?, ?, ?, ?, ?, ?)",
                              (memory_id, kind, session, when, text, terms))
            self.conn.executemany(
                "INSERT OR IGNORE INTO memory_cites(memory_id, path, blob_oid, start_line, end_line) VALUES (?, ?, ?, ?, ?)",
                ((memory_id, cite.path, cite.blob_oid, cite.start_line, cite.end_line) for cite in cites or ()))
        if vector is not None:
            self.add_vector(memory_id, session, text, vector, when)
        return memory_id

    def add_vector(self, memory_id: str, session: str, text: str, vector, when: int) -> bool:
        """Give a kept memory its vector in the store (now, or later for one kept without). False when there is no
        store or it failed: the memory is kept either way."""
        if self.store is None:
            return False
        try:
            event = self.store.append(memory_id, session, text, vector, when)
        except Exception:  # noqa: BLE001 - the store is a convenience; the memory is already kept
            return False
        self.conn.execute("UPDATE memories SET embedded = 1, aeon_id = ? WHERE memory_id = ? AND deleted = 0",
                          (event if isinstance(event, int) else None, memory_id))
        return True

    def pending_vectors(self, limit: int = 200) -> list[tuple[str, str, str, int]]:
        """Memories kept without a vector (the worker was not running, or the store was installed later), oldest
        first: (memory_id, session, text, created_at)."""
        return [tuple(row) for row in self.conn.execute(
            "SELECT memory_id, session, text, created_at FROM memories WHERE deleted = 0 AND embedded = 0 "
            "ORDER BY created_at, memory_id LIMIT ?", (limit,))]

    def recall(self, query: str, vector=None, k: int = 5, view: View | None = None) -> list[Recalled]:
        """The memories that fit the query best: by meaning when a vector and a store are
        there, by shared words, and newest first among equals. With a view, each says
        whether what it cites has changed."""
        rankings: list[list[str]] = []
        if vector is not None and self.store is not None:
            try:
                # Each id once: a store that replayed its log may hold an event twice.
                rankings.append(list(dict.fromkeys(self.store.nearest(vector, max(k * OVER_ASK, 50)))))
            except Exception:  # noqa: BLE001 - recall goes on by words
                pass
        rows = self.conn.execute(
            "SELECT memory_id, kind, session, created_at, text, terms FROM memories WHERE deleted = 0 "
            "ORDER BY created_at DESC, memory_id LIMIT ?", (WORD_POOL,)).fetchall()
        live = {row[0]: row for row in rows}
        # What the store proposes is looked up by itself: a memory older than the newest
        # WORD_POOL is out of reach of a match by words, not of a match by meaning.
        proposed = [memory_id for ranking in rankings for memory_id in ranking if memory_id not in live]
        for start in range(0, len(proposed), 500):
            part = proposed[start:start + 500]
            for row in self.conn.execute(
                    "SELECT memory_id, kind, session, created_at, text, terms FROM memories "
                    f"WHERE deleted = 0 AND memory_id IN ({','.join('?' * len(part))})", part):
                live[row[0]] = row
        wanted = set(lexical.query_terms(query))
        if wanted:
            overlap = {row[0]: len(wanted & set(row[5].split())) for row in rows}
            rankings.append([memory_id for memory_id, count in sorted(overlap.items(), key=lambda item: -item[1]) if count])
        scores: dict[str, float] = {}
        for ranking in rankings:
            position = 0
            for memory_id in ranking:
                if memory_id in live:  # unknown to the catalogue, or forgotten: not a memory
                    position += 1
                    scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (_K + position)
        chosen = sorted(scores, key=lambda memory_id: (-scores[memory_id], -live[memory_id][3], memory_id))[:k]
        if not chosen and not wanted and vector is None:
            chosen = [row[0] for row in rows[:k]]  # nothing was asked: the newest
        return [self._recalled(live[memory_id], view) for memory_id in chosen]

    def _recalled(self, row: tuple, view: View | None) -> Recalled:
        cites = []
        for path, blob_oid, start, end in self.conn.execute(
                "SELECT path, blob_oid, start_line, end_line FROM memory_cites WHERE memory_id = ? ORDER BY path, start_line",
                (row[0],)):
            cites.append(Cite(path, blob_oid, start, end, stale=view is not None and view.files.get(path) != blob_oid))
        return Recalled(row[0], row[1], row[2], row[3], row[4], tuple(cites), any(cite.stale for cite in cites))

    def forget(self, memory_id: str) -> bool:
        """Remove a memory's text and never recall it again, and erase its event in the vector store where the store
        can (Aeon with tombstones: the event is left out of its search and its text overwritten). The catalogue is what
        recall obeys in any case."""
        found = self.conn.execute("SELECT aeon_id FROM memories WHERE memory_id = ? AND deleted = 0", (memory_id,)).fetchall()
        with transaction(self.conn):
            self.conn.execute("UPDATE memories SET deleted = 1, text = '', terms = '' WHERE memory_id = ? AND deleted = 0", (memory_id,))
            changed = self.conn.changes()
            self.conn.execute("DELETE FROM memory_cites WHERE memory_id = ?", (memory_id,))
        self._scrub()
        if found and found[0][0] is not None and hasattr(self.store, "erase"):
            try:
                self.store.erase([found[0][0]])
            except Exception:  # noqa: BLE001 - the catalogue has forgotten it; the store is best effort
                pass
        return bool(changed)

    def forget_all(self) -> int:
        with transaction(self.conn):
            self.conn.execute("UPDATE memories SET deleted = 1, text = '', terms = '' WHERE deleted = 0")
            changed = self.conn.changes()
            self.conn.execute("DELETE FROM memory_cites")
        self._scrub()
        if hasattr(self.store, "clear"):
            try:
                self.store.clear()     # every event, text and vector: the store's files go
            except Exception:  # noqa: BLE001
                pass
        return changed

    def _scrub(self) -> None:
        """After a forget: empty the write-ahead log, where the old page with the text would
        otherwise stay until some later checkpoint. Best effort: another session reading at
        this moment postpones it to the next one."""
        try:
            self.conn.pragma("wal_checkpoint", "TRUNCATE")
        except apsw.Error:
            pass

    def list(self, limit: int = 50) -> list[dict]:
        return [{"memory_id": row[0], "kind": row[1], "session": row[2], "created_at": row[3], "text": row[4]}
                for row in self.conn.execute(
                    "SELECT memory_id, kind, session, created_at, text FROM memories WHERE deleted = 0 "
                    "ORDER BY created_at DESC, memory_id LIMIT ?", (limit,))]

    def counts(self) -> dict[str, int]:
        kept, embedded = self.conn.execute(
            "SELECT count(*), coalesce(sum(embedded), 0) FROM memories WHERE deleted = 0").fetchall()[0]
        return {"memories": kept, "with_vector": embedded}


# ---- Aeon ------------------------------------------------------------------------------------------

_loaded: dict[str, object] = {}


def _aeon_core():
    """Aeon's extension module, or None. Found in the directory `ALEPH_AEON_LIB` names (a
    build of Aeon's core: a file `core.cpython-*.so`), else in the build `aleph setup --memory`
    made in the cache directory, else as the installed package. The extension is loaded from
    its own file under a name of its own: nothing is put on the import path, and an unrelated
    module that happens to be called `core` is never taken for it."""
    from . import aeon_setup

    directory = os.environ.get("ALEPH_AEON_LIB") or (str(aeon_setup.lib_dir()) if aeon_setup.installed() else "")
    if directory:
        if directory not in _loaded:
            found = None
            for candidate in sorted(Path(directory).glob("core.cpython-*.so")) + sorted(Path(directory).glob("core.*.pyd")):
                try:
                    spec = importlib.util.spec_from_file_location("core", candidate)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                except Exception:  # noqa: BLE001 - built for another Python, or not Aeon at all
                    continue
                if hasattr(module, "TraceManager"):
                    found = module
                    break
            _loaded[directory] = found
        return _loaded[directory]
    try:
        from aeon_py import core as packaged
        return packaged if hasattr(packaged, "TraceManager") else None
    except Exception:  # noqa: BLE001 - not installed, or installed without its heavy dependencies
        return None


def open_store(state: Path) -> "AeonStore | None":
    """The vector store for the repository whose state directory is `state`, or None when
    Aeon is not there."""
    core = _aeon_core()
    if core is None:
        return None
    return AeonStore(core, state / "memory" / "trace")


def _dimension(trace) -> int:
    """The size of the vectors a store holds; 0 before the first. A property in some builds
    of Aeon's bindings and a method in others."""
    found = trace.embedding_dim
    return int(found() if callable(found) else found)


class AeonStore:
    """Aeon's Trace store, one owner at a time.

    Two processes with the same store open corrupt it (ADR-006), so every use takes an
    exclusive file lock, opens the store, does one thing, drops the object (it has no
    close) and unlocks. The probe measured 2 ms for that at 5,000 events and 11 ms at
    50,000. Aleph's memory id is the first line of each event's text: Aeon's own ids are
    reassigned when its log is replayed after a crash."""

    def __init__(self, core, path: Path) -> None:
        self.core = core
        self.path = path

    def _with(self, operation, create: bool = False):
        if not create and not self.path.parent.is_dir():
            return None  # nothing was ever stored: reading must not leave a directory behind
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(f"{self.path}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                trace = self.core.TraceManager(str(self.path))
                try:
                    return operation(trace)
                finally:
                    del trace
                    gc.collect()  # the destructor closes the files; it must run before the lock goes
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def append(self, memory_id: str, session: str, text: str, vector, when: int) -> int | None:
        """Keep the vector and text; returns Aeon's id of the event (kept by the catalogue, for erasing it)."""
        values = [float(value) for value in vector]
        # Session ids are cut to 35 bytes inside Aeon and then mismatch; 32 are kept here.
        event = self._with(lambda trace: trace.append_event(session[:32], 1, f"{memory_id}\n{text}", embedding=values,
                                                            event_time=int(when) * 1_000_000), create=True)
        return int(event) if isinstance(event, int) else None

    def can_erase(self) -> bool:
        """This build of Aeon can tombstone an event and overwrite its text (Aeon's branch with Aleph's fixes)."""
        return hasattr(self.core.TraceManager, "tombstone_event")

    def erase(self, event_ids) -> None:
        if not self.can_erase():
            return
        self._with(lambda trace: [trace.tombstone_event(int(event), erase_text=True) for event in event_ids])

    def clear(self) -> None:
        """Remove the store's files (the events, their texts and vectors, the log), under the store's lock."""
        if not self.path.parent.is_dir():
            return
        with open(f"{self.path}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                for suffix in ("", ".blobs", ".wal"):
                    Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def nearest(self, vector, limit: int) -> list[str]:
        values = [float(value) for value in vector]

        def search(trace) -> list[str]:
            if _dimension(trace) not in (0, len(values)):
                return []  # Aeon would answer nothing, silently, for a vector of another size
            return [event["text"].split("\n", 1)[0] for event in trace.semantic_search(values, limit)]

        return self._with(search) or []

    def ids(self) -> set[str]:
        """The memory ids of every event that has a vector: what the catalogue is checked against."""
        def read(trace) -> set[str]:
            dim = _dimension(trace)
            if not dim:
                return set()
            probe = [0.0] * dim
            probe[0] = 1.0
            return {event["text"].split("\n", 1)[0] for event in trace.semantic_search(probe, max(trace.size(), 1))}

        return self._with(read) or set()
