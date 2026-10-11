"""Bringing the index up to date with a view, and telling queries which chunks they may return."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import PurePosixPath

import apsw

from . import filters, gitio, lexical
from .chunking import CHUNKER_VERSION, Chunk, chunk_file, chunk_hash
from .store import transaction
from .view import View

BATCH = 50
SEEN_REFRESH_SECONDS = 3600


@dataclass(frozen=True)
class SyncReport:
    indexed: int    # blobs chunked and stored by this call
    skipped: int    # blobs recorded as not indexable (binary, too large, ...)
    pending: int    # blobs in the view still not indexed: the time budget ran out
    seconds: float


class Index:
    """One connection to a repository's index, and the view it is currently answering for."""

    def __init__(self, conn: apsw.Connection) -> None:
        self.conn = conn
        self._fingerprint: str | None = None
        self._pending = 0
        self._path_tokens: dict[str, tuple[frozenset[str], frozenset[str]]] = {}

    def sync(self, view: View, budget_seconds: float = 5.0) -> SyncReport:
        """Index every blob of the view the index has not seen, within a time budget,
        then point the connection's view tables at this view."""
        started = time.monotonic()
        if view.fingerprint == self._fingerprint and self._pending == 0:
            return SyncReport(0, 0, 0, time.monotonic() - started)
        if view.fingerprint != self._fingerprint:
            self._load_view_blobs(view)
        missing = [row[0] for row in self.conn.execute(
            "SELECT v.blob_oid FROM temp.view_blobs v LEFT JOIN blobs b "
            "ON b.blob_oid = v.blob_oid AND b.chunker_version = ? WHERE b.blob_oid IS NULL ORDER BY v.blob_oid",
            (CHUNKER_VERSION,),
        )]
        indexed = skipped = done = 0
        deadline = started + budget_seconds
        with gitio.BlobReader(view.root) as reader:
            for offset in range(0, len(missing), BATCH):
                if time.monotonic() >= deadline:
                    break
                batch = missing[offset:offset + BATCH]
                prepared = [self._prepare(view, reader, oid) for oid in batch]
                with transaction(self.conn):
                    for oid, kind, size, chunks in prepared:
                        self._store(oid, kind, size, chunks)
                        if chunks:
                            indexed += 1
                        else:
                            skipped += 1
                done += len(batch)
        self._pending = len(missing) - done
        self._refresh_view_chunks()
        self._touch()
        self._fingerprint = view.fingerprint
        return SyncReport(indexed, skipped, self._pending, time.monotonic() - started)

    def path_tokens(self, view: View) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
        """For each path of the view: (all its lower-cased words, the words of its file name)."""
        if not self._path_tokens or self._fingerprint != view.fingerprint:
            self._path_tokens = {
                path: (frozenset(lexical.query_terms(path)), frozenset(lexical.query_terms(PurePosixPath(path).stem)))
                for path in view.files
            }
        return self._path_tokens

    def counts(self) -> dict[str, int]:
        one = lambda sql: self.conn.execute(sql).fetchall()[0][0]  # noqa: E731
        return {
            "blobs_in_view": one("SELECT count(*) FROM temp.view_blobs"),
            "chunks_in_view": one("SELECT count(*) FROM temp.view_chunks"),
            "blobs_total": one("SELECT count(*) FROM blobs"),
            "chunks_total": one("SELECT count(*) FROM chunks"),
            "pending": self._pending,
        }

    def _load_view_blobs(self, view: View) -> None:
        self._path_tokens = {}
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM temp.view_blobs")
        self.conn.executemany("INSERT OR IGNORE INTO temp.view_blobs(blob_oid) VALUES (?)",
                              ((oid,) for oid in set(view.files.values())))
        self.conn.execute("COMMIT")

    def _refresh_view_chunks(self) -> None:
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM temp.view_chunks")
        self.conn.execute(
            "INSERT INTO temp.view_chunks(chunk_id) SELECT DISTINCT bc.chunk_id FROM blob_chunks bc "
            "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid"
        )
        self.conn.execute("COMMIT")

    def _touch(self) -> None:
        """Mark the view's blobs as recently seen, at most once an hour each, so a
        query does not take the write lock every time."""
        now = int(time.time())
        stale = self.conn.execute(
            "SELECT 1 FROM blobs b JOIN temp.view_blobs v ON v.blob_oid = b.blob_oid "
            "WHERE b.last_seen_at < ? LIMIT 1", (now - SEEN_REFRESH_SECONDS,),
        ).fetchall()
        if stale:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE blobs SET last_seen_at = ? WHERE last_seen_at < ? "
                    "AND blob_oid IN (SELECT blob_oid FROM temp.view_blobs)",
                    (now, now - SEEN_REFRESH_SECONDS),
                )

    @staticmethod
    def _prepare(view: View, reader: gitio.BlobReader, oid: str) -> tuple[str, str, int, list[Chunk]]:
        path = view.paths_for(oid)[0]
        data = view.read(path, reader)
        if data is None:
            return oid, "skip:unreadable", 0, []
        kind, text = filters.classify(path, data)
        if text is None:
            return oid, kind, len(data), []
        chunks = chunk_file(path, kind, text)
        return oid, (kind if chunks else "skip:empty"), len(data), chunks

    def _store(self, oid: str, kind: str, size: int, chunks: list[Chunk]) -> None:
        conn = self.conn
        conn.execute("DELETE FROM blobs WHERE blob_oid = ? AND chunker_version != ?", (oid, CHUNKER_VERSION))
        conn.execute(
            "INSERT OR IGNORE INTO blobs(blob_oid, kind, size, chunker_version, last_seen_at) VALUES (?, ?, ?, ?, ?)",
            (oid, kind, size, CHUNKER_VERSION, int(time.time())),
        )
        if conn.changes() == 0:
            return  # another session indexed this blob first
        for ordinal, chunk in enumerate(chunks):
            digest = chunk_hash(kind, chunk)
            conn.execute(
                "INSERT INTO chunks(chunk_hash, kind, symbol, signature, breadcrumb, text) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chunk_hash) DO NOTHING",
                (digest, chunk.kind, chunk.symbol, chunk.signature, chunk.breadcrumb, chunk.text),
            )
            if conn.changes() == 1:
                chunk_id = conn.last_insert_rowid()
                lexical.insert(conn, chunk_id, chunk)
            else:  # the same content is already indexed, from another file or branch
                chunk_id = conn.execute("SELECT chunk_id FROM chunks WHERE chunk_hash = ?", (digest,)).fetchall()[0][0]
            conn.execute(
                "INSERT INTO blob_chunks(blob_oid, ord, chunk_id, start_line, end_line) VALUES (?, ?, ?, ?, ?)",
                (oid, ordinal, chunk_id, chunk.start_line, chunk.end_line),
            )


def collect_garbage(conn: apsw.Connection, max_age_days: float, now: float | None = None) -> dict[str, int]:
    """Drop blobs no view has used for `max_age_days`, then the chunks nothing refers to.
    Blobs of the connection's current view are always kept."""
    cutoff = int((time.time() if now is None else now) - max_age_days * 86400)
    with transaction(conn):
        conn.execute(
            "DELETE FROM blobs WHERE last_seen_at < ? AND blob_oid NOT IN (SELECT blob_oid FROM temp.view_blobs)",
            (cutoff,),
        )
        blobs = conn.changes()
        orphans = [row[0] for row in conn.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id NOT IN (SELECT chunk_id FROM blob_chunks)")]
        conn.executemany("DELETE FROM chunk_fts WHERE rowid = ?", ((cid,) for cid in orphans))
        conn.executemany("DELETE FROM chunks WHERE chunk_id = ?", ((cid,) for cid in orphans))
    return {"blobs_removed": blobs, "chunks_removed": len(orphans)}
