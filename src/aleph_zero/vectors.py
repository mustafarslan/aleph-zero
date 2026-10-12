"""Embedding vectors: a file of fixed-size rows beside the index, mapped into memory.

The file is shared by every session through the operating system's page cache, so it
costs each of them almost no private memory. Which chunk owns which row is in the
database (`chunk_vectors`). The two are kept consistent by one rule: a row's bytes
are written at their own offset and flushed to disk *inside* the index's write
transaction, before it commits. A row the database can see therefore always has its
bytes, in every process, and after a crash.

Rows are never moved and never reused: another session has the file mapped. Garbage
collection only removes the database entry (it goes with its chunk). Search reads
just the rows of the current view, so dead rows cost disk space, not time.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import apsw
import numpy as np

from .indexer import Index
from .store import transaction

WIDTH = 256
ROW_BYTES = WIDTH * 2       # float16
_BLOCK = 16_384             # rows converted to float32 at a time: 16 MB of scratch, whatever the view's size
# Which chunks get a vector: code and documentation. A 'block' is a window of a file with
# no language profile (JSON, YAML, SQL fixtures, templates); such files are searched by
# their words only. In two repositories measured they were 58% and 72% of all text to
# embed, and on the Loc-Bench pilot leaving them out found more of the right files, not
# fewer (ADR-001).
EMBEDDED = "c.kind != 'block'"


class VectorStore:
    """The vectors of one kind (model, prefix version, width) for one repository."""

    def __init__(self, conn: apsw.Connection, directory: Path, kind: str) -> None:
        self.conn = conn
        self.kind = kind
        self.path = Path(directory) / f"vectors.{re.sub(r'[^A-Za-z0-9._-]+', '_', kind)}.f16"
        self._map: np.memmap | None = None
        self._cache: tuple | None = None    # (key, chunk ids, rows) of the view last searched
        self._coverage: tuple | None = None  # (key, (with a vector, should have one))
        self._recover()

    # ---- writing ---------------------------------------------------------------------------

    def add(self, chunk_ids: list[int], matrix: np.ndarray, hashes: list[str] | None = None) -> int:
        """Store one unit-length row per chunk. Chunks that already have a vector of this
        kind are skipped (another session embedded them first). With `hashes`, the content
        hash each chunk had when its text was read: a chunk id is reused after garbage
        collection, and a vector made from the old content must not land on the new.
        Returns how many were stored."""
        if len(chunk_ids) != len(matrix):
            raise ValueError("one vector per chunk")
        if not chunk_ids:
            return 0
        half = np.ascontiguousarray(matrix, dtype=np.float16)
        with transaction(self.conn):
            marks = ",".join("?" * len(chunk_ids))
            have = {row[0] for row in self.conn.execute(
                f"SELECT chunk_id FROM chunk_vectors WHERE kind = ? AND chunk_id IN ({marks})", (self.kind, *chunk_ids))}
            alive = dict(self.conn.execute(
                f"SELECT chunk_id, chunk_hash FROM chunks WHERE chunk_id IN ({marks})", chunk_ids))
            new = [(position, chunk_id) for position, chunk_id in enumerate(chunk_ids)
                   if chunk_id not in have and chunk_id in alive
                   and (hashes is None or alive[chunk_id] == hashes[position])]
            if not new:
                return 0
            first = self.conn.execute("SELECT next_row FROM vector_files WHERE kind = ?", (self.kind,)).fetchall()
            start = first[0][0] if first else 0
            self.conn.execute(
                "INSERT INTO vector_files(kind, next_row) VALUES (?, ?) "
                "ON CONFLICT(kind) DO UPDATE SET next_row = excluded.next_row", (self.kind, start + len(new)))
            payload = half[[position for position, _ in new]].tobytes()
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                os.pwrite(descriptor, payload, start * ROW_BYTES)
                os.fsync(descriptor)  # on disk before the database says the rows exist
            finally:
                os.close(descriptor)
            self.conn.executemany(
                "INSERT INTO chunk_vectors(chunk_id, kind, row) VALUES (?, ?, ?)",
                ((chunk_id, self.kind, start + offset) for offset, (_, chunk_id) in enumerate(new)))
        return len(new)

    # ---- reading ---------------------------------------------------------------------------

    def missing(self, index: Index, limit: int | None = None) -> list[int]:
        """Chunks of the current view that should have a vector of this kind and do not,
        in chunk order."""
        return [row[0] for row in index.conn.execute(
            "SELECT v.chunk_id FROM temp.view_chunks v JOIN chunks c ON c.chunk_id = v.chunk_id "
            "LEFT JOIN chunk_vectors cv ON cv.chunk_id = v.chunk_id AND cv.kind = ? "
            f"WHERE cv.chunk_id IS NULL AND {EMBEDDED} ORDER BY v.chunk_id LIMIT ?",
            (self.kind, -1 if limit is None else limit))]

    def coverage(self, index: Index) -> tuple[int, int]:
        """(chunks of the view with a vector, chunks of the view that should have one).
        Counted once per state of the view and of the stored vectors: it is asked on every
        query, and the count took 38 ms on a 51,000-chunk view."""
        key = (index._view_epoch, self._stored())
        if self._coverage is None or self._coverage[0] != key:
            counted = index.conn.execute(
                "SELECT count(cv.chunk_id), count(*) FROM temp.view_chunks v JOIN chunks c ON c.chunk_id = v.chunk_id "
                f"LEFT JOIN chunk_vectors cv ON cv.chunk_id = v.chunk_id AND cv.kind = ? WHERE {EMBEDDED}",
                (self.kind,)).fetchall()[0]
            self._coverage = (key, counted)
        return self._coverage[1]

    def _stored(self) -> int:
        """How many rows were ever given out for this kind: changes whenever a vector is added."""
        found = self.conn.execute("SELECT next_row FROM vector_files WHERE kind = ?", (self.kind,)).fetchall()
        return found[0][0] if found else 0

    def search(self, index: Index, query: np.ndarray, limit: int, scoped: bool = False) -> list[tuple[int, float]]:
        """(chunk id, cosine similarity) of the nearest chunks of the view, best first.
        With `scoped`, only chunks in temp.scope_chunks."""
        chunk_ids, rows = self._view_rows(index, scoped)
        if not len(rows):
            return []
        mapped = self._mapped(int(rows.max()) + 1)
        if mapped is None:
            return []
        query = np.asarray(query, dtype=np.float32)
        scores = np.empty(len(rows), dtype=np.float32)
        for start in range(0, len(rows), _BLOCK):
            part = rows[start:start + _BLOCK]
            scores[start:start + len(part)] = mapped[part].astype(np.float32) @ query
        top = min(limit, len(scores))
        best = np.argpartition(-scores, top - 1)[:top]
        best = best[np.argsort(-scores[best], kind="stable")]
        return [(int(chunk_ids[position]), float(scores[position])) for position in best]

    def _view_rows(self, index: Index, scoped: bool) -> tuple[np.ndarray, np.ndarray]:
        """The view's chunks that have a vector, and their rows, in row order. Remembered
        until the view or the set of stored vectors changes."""
        key = (index._view_epoch, index._scope if scoped else None, self._stored())
        if self._cache is None or self._cache[0] != key:
            table = "scope_chunks" if scoped else "view_chunks"
            pairs = index.conn.execute(
                f"SELECT cv.chunk_id, cv.row FROM chunk_vectors cv JOIN temp.{table} v ON v.chunk_id = cv.chunk_id "
                f"JOIN chunks c ON c.chunk_id = cv.chunk_id WHERE cv.kind = ? AND {EMBEDDED} ORDER BY cv.row",
                (self.kind,)).fetchall()
            found = np.asarray(pairs, dtype=np.int64).reshape(-1, 2)
            self._cache = (key, found[:, 0], found[:, 1])
        return self._cache[1], self._cache[2]

    def _mapped(self, needed_rows: int) -> np.memmap | None:
        """The file, mapped for at least `needed_rows`. Mapped again when it has grown."""
        if self._map is None or len(self._map) < needed_rows:
            self._map = None
            try:
                on_disk = os.path.getsize(self.path) // ROW_BYTES
            except OSError:
                return None
            if on_disk < needed_rows:
                return None
            self._map = np.memmap(self.path, dtype=np.float16, mode="r", shape=(on_disk, WIDTH))
        return self._map

    def _recover(self) -> None:
        """Forget rows the file does not hold. The flush before commit makes this rare: it
        takes a file that was truncated or replaced, or a database restored without it."""
        def rows_on_disk() -> int:
            try:
                return os.path.getsize(self.path) // ROW_BYTES
            except OSError:
                return 0

        beyond = self.conn.execute(
            "SELECT 1 FROM chunk_vectors WHERE kind = ? AND row >= ? LIMIT 1", (self.kind, rows_on_disk())).fetchall()
        if beyond:
            with transaction(self.conn):
                # Measured again under the write lock: a session that was adding rows while we
                # looked has finished, and its rows must not be taken for missing ones.
                self.conn.execute("DELETE FROM chunk_vectors WHERE kind = ? AND row >= ?", (self.kind, rows_on_disk()))
