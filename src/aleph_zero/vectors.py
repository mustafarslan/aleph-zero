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


class VectorStore:
    """The vectors of one kind (model, prefix version, width) for one repository."""

    def __init__(self, conn: apsw.Connection, directory: Path, kind: str) -> None:
        self.conn = conn
        self.kind = kind
        self.path = Path(directory) / f"vectors.{re.sub(r'[^A-Za-z0-9._-]+', '_', kind)}.f16"
        self._map: np.memmap | None = None
        self._cache: tuple | None = None    # (key, chunk ids, rows) of the view last searched
        self._recover()

    # ---- writing ---------------------------------------------------------------------------

    def add(self, chunk_ids: list[int], matrix: np.ndarray) -> int:
        """Store one unit-length row per chunk. Chunks that already have a vector of this
        kind are skipped (another session embedded them first). Returns how many were stored."""
        if len(chunk_ids) != len(matrix):
            raise ValueError("one vector per chunk")
        if not chunk_ids:
            return 0
        half = np.ascontiguousarray(matrix, dtype=np.float16)
        with transaction(self.conn):
            marks = ",".join("?" * len(chunk_ids))
            have = {row[0] for row in self.conn.execute(
                f"SELECT chunk_id FROM chunk_vectors WHERE kind = ? AND chunk_id IN ({marks})", (self.kind, *chunk_ids))}
            alive = {row[0] for row in self.conn.execute(
                f"SELECT chunk_id FROM chunks WHERE chunk_id IN ({marks})", chunk_ids)}
            new = [(position, chunk_id) for position, chunk_id in enumerate(chunk_ids)
                   if chunk_id not in have and chunk_id in alive]
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
        """Chunks of the current view that have no vector of this kind, in chunk order."""
        return [row[0] for row in index.conn.execute(
            "SELECT v.chunk_id FROM temp.view_chunks v LEFT JOIN chunk_vectors cv "
            "ON cv.chunk_id = v.chunk_id AND cv.kind = ? WHERE cv.chunk_id IS NULL ORDER BY v.chunk_id LIMIT ?",
            (self.kind, -1 if limit is None else limit))]

    def coverage(self, index: Index) -> tuple[int, int]:
        """(chunks of the view with a vector, chunks of the view)."""
        return index.conn.execute(
            "SELECT count(cv.chunk_id), count(*) FROM temp.view_chunks v LEFT JOIN chunk_vectors cv "
            "ON cv.chunk_id = v.chunk_id AND cv.kind = ?", (self.kind,)).fetchall()[0]

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
        stored = self.conn.execute("SELECT next_row FROM vector_files WHERE kind = ?", (self.kind,)).fetchall()
        key = (index._view_epoch, index._scope if scoped else None, stored[0][0] if stored else 0)
        if self._cache is None or self._cache[0] != key:
            table = "scope_chunks" if scoped else "view_chunks"
            pairs = index.conn.execute(
                f"SELECT cv.chunk_id, cv.row FROM chunk_vectors cv JOIN temp.{table} v ON v.chunk_id = cv.chunk_id "
                "WHERE cv.kind = ? ORDER BY cv.row", (self.kind,)).fetchall()
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
        try:
            on_disk = os.path.getsize(self.path) // ROW_BYTES
        except OSError:
            on_disk = 0
        beyond = self.conn.execute(
            "SELECT 1 FROM chunk_vectors WHERE kind = ? AND row >= ? LIMIT 1", (self.kind, on_disk)).fetchall()
        if beyond:
            with transaction(self.conn):
                self.conn.execute("DELETE FROM chunk_vectors WHERE kind = ? AND row >= ?", (self.kind, on_disk))
