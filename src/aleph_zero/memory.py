"""Long-term memory: what was asked, answered and noted about a repository in earlier sessions.

The catalogue is a table in the index (`memories`): it is the authority for what exists,
what it cites and what was forgotten. Beside it sits a store of vectors for recall by
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
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import apsw

from . import lexical
from .store import transaction
from .view import View

KINDS = ("chat", "note")
OVER_ASK = 20        # Aeon's search has no filter: ask for this many times what is wanted
WORD_POOL = 2000     # the newest memories looked at for a match by words
_K = 60              # reciprocal rank fusion, as in search


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
        with transaction(self.conn):
            self.conn.execute("INSERT INTO memories(memory_id, kind, session, created_at, text) VALUES (?, ?, ?, ?, ?)",
                              (memory_id, kind, session, when, text))
            self.conn.executemany(
                "INSERT OR IGNORE INTO memory_cites(memory_id, path, blob_oid, start_line, end_line) VALUES (?, ?, ?, ?, ?)",
                ((memory_id, cite.path, cite.blob_oid, cite.start_line, cite.end_line) for cite in cites or ()))
        if vector is not None and self.store is not None:
            try:
                self.store.append(memory_id, session, text, vector, when)
            except Exception:  # noqa: BLE001 - the store is a convenience; the memory is already kept
                return memory_id
            self.conn.execute("UPDATE memories SET embedded = 1 WHERE memory_id = ?", (memory_id,))
        return memory_id

    def recall(self, query: str, vector=None, k: int = 5, view: View | None = None) -> list[Recalled]:
        """The memories that fit the query best: by meaning when a vector and a store are
        there, by shared words, and newest first among equals. With a view, each says
        whether what it cites has changed."""
        rankings: list[list[str]] = []
        if vector is not None and self.store is not None:
            try:
                rankings.append(self.store.nearest(vector, max(k * OVER_ASK, 50)))
            except Exception:  # noqa: BLE001 - recall goes on by words
                pass
        rows = self.conn.execute(
            "SELECT memory_id, kind, session, created_at, text FROM memories WHERE deleted = 0 "
            "ORDER BY created_at DESC, memory_id LIMIT ?", (WORD_POOL,)).fetchall()
        live = {row[0]: row for row in rows}
        wanted = set(lexical.query_terms(query))
        if wanted:
            overlap = {row[0]: len(wanted & set(lexical.query_terms(row[4]))) for row in rows}
            rankings.append([memory_id for memory_id, count in sorted(overlap.items(), key=lambda item: -item[1]) if count])
        scores: dict[str, float] = {}
        for ranking in rankings:
            position = 0
            for memory_id in ranking:
                if memory_id in live:  # unknown to the catalogue, or forgotten: not a memory
                    position += 1
                    scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (_K + position)
        newest = {row[0]: position for position, row in enumerate(rows)}
        chosen = sorted(scores, key=lambda memory_id: (-scores[memory_id], newest[memory_id]))[:k]
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
        """Remove a memory's text and never recall it again. The vector store may keep its
        event until it can delete one (ADR-006); the catalogue is what recall obeys."""
        with transaction(self.conn):
            self.conn.execute("UPDATE memories SET deleted = 1, text = '' WHERE memory_id = ? AND deleted = 0", (memory_id,))
            changed = self.conn.changes()
            self.conn.execute("DELETE FROM memory_cites WHERE memory_id = ?", (memory_id,))
        return bool(changed)

    def forget_all(self) -> int:
        with transaction(self.conn):
            self.conn.execute("UPDATE memories SET deleted = 1, text = '' WHERE deleted = 0")
            changed = self.conn.changes()
            self.conn.execute("DELETE FROM memory_cites")
        return changed

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

def _aeon_core():
    """Aeon's extension module, or None. Found in the directory `ALEPH_AEON_LIB` names (a
    build of Aeon's core), else as the installed package. Aleph never builds or installs it."""
    directory = os.environ.get("ALEPH_AEON_LIB")
    if directory:
        if not list(Path(directory).glob("core.*")):
            return None
        if directory not in sys.path:
            sys.path.insert(0, directory)
        try:
            import core  # the extension alone: the package around it imports far more
            return core if hasattr(core, "TraceManager") else None
        except ImportError:
            return None
    try:
        from aeon_py import core as packaged
        return packaged
    except Exception:  # noqa: BLE001 - not installed, or installed without its heavy dependencies
        return None


def open_store(state: Path) -> "AeonStore | None":
    """The vector store for the repository whose state directory is `state`, or None when
    Aeon is not there."""
    core = _aeon_core()
    if core is None:
        return None
    directory = state / "memory"
    directory.mkdir(parents=True, exist_ok=True)
    return AeonStore(core, directory / "trace")


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

    def _with(self, operation):
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

    def append(self, memory_id: str, session: str, text: str, vector, when: int) -> None:
        values = [float(value) for value in vector]
        # Session ids are cut to 35 bytes inside Aeon and then mismatch; 32 are kept here.
        self._with(lambda trace: trace.append_event(session[:32], 1, f"{memory_id}\n{text}", embedding=values,
                                                    event_time=int(when) * 1_000_000) and None)

    def nearest(self, vector, limit: int) -> list[str]:
        values = [float(value) for value in vector]

        def search(trace) -> list[str]:
            if _dimension(trace) not in (0, len(values)):
                return []  # Aeon would answer nothing, silently, for a vector of another size
            return [event["text"].split("\n", 1)[0] for event in trace.semantic_search(values, limit)]

        return self._with(search)

    def ids(self) -> set[str]:
        """The memory ids of every event that has a vector: what the catalogue is checked against."""
        def read(trace) -> set[str]:
            dim = _dimension(trace)
            if not dim:
                return set()
            probe = [0.0] * dim
            probe[0] = 1.0
            return {event["text"].split("\n", 1)[0] for event in trace.semantic_search(probe, max(trace.size(), 1))}

        return self._with(read)
