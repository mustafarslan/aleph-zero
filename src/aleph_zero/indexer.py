"""Bringing the index up to date with a view, and telling queries which chunks they may return."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

import apsw

from . import filters, gitio, lexical
from .chunking import CHUNKER_VERSION, Chunk, chunk_file, chunk_hash
from .store import transaction
from .view import View

BATCH = 50
SEEN_REFRESH_SECONDS = 3600
RETRY_SECONDS = 60  # before a blob git could not supply is asked for again under the same view

Key = tuple[str, str]  # (blob id, family): the same bytes are chunked once per treatment


@dataclass(frozen=True)
class SyncReport:
    indexed: int    # blobs chunked and stored by this call
    skipped: int    # blobs recorded as not indexable (binary, too large, ...)
    pending: int    # blobs in the view still not indexed: out of time, or not readable right now
    seconds: float


class Index:
    """One connection to a repository's index, and the view it is currently answering for."""

    def __init__(self, conn: apsw.Connection) -> None:
        self.conn = conn
        self._fingerprint: str | None = None
        self._pending = 0
        self._missing: frozenset[Key] = frozenset()  # unindexed blobs of the view at the last sync
        self._deferred: dict[Key, float] = {}  # blob git does not have -> when we last asked
        self._paths: dict[Key, list[str]] = {}
        self._path_tokens: dict[str, tuple[frozenset[str], frozenset[str]]] | None = None
        self._view_epoch = 0  # goes up whenever temp.view_chunks is rebuilt
        self._scope: tuple | None = None

    def sync(self, view: View, budget_seconds: float = 5.0) -> SyncReport:
        """Index every blob of the view the index has not seen, within a time budget,
        then point the connection's view tables at this view.

        Cheap when nothing changed, but never skipped: another session may have
        collected this view's content, or indexed what was pending here."""
        started = time.monotonic()
        changed = view.fingerprint != self._fingerprint
        if changed:
            self._fingerprint = None  # until the tables below match the view again
            self._load_view_blobs(view)
        missing: list[Key] = [(row[0], row[1]) for row in self.conn.execute(
            "SELECT v.blob_oid, v.family FROM temp.view_blobs v LEFT JOIN blobs b "
            "ON b.blob_oid = v.blob_oid AND b.family = v.family AND b.chunker_version = ? "
            "WHERE b.blob_oid IS NULL ORDER BY v.blob_oid, v.family",
            (CHUNKER_VERSION,),
        )]
        retry_before = started - RETRY_SECONDS
        todo = [key for key in missing if self._deferred.get(key, retry_before) <= retry_before]
        indexed = skipped = 0
        left = len(missing)
        stored: set[Key] = set()
        deadline = started + budget_seconds
        with gitio.BlobReader(view.root) as reader:
            for offset in range(0, len(todo), BATCH):
                if time.monotonic() >= deadline:
                    break
                prepared = []
                for key in todo[offset:offset + BATCH]:
                    item = self._prepare(view, reader, key)
                    if item is not None:
                        prepared.append(item)
                    elif not any(path in view.dirty for path in self._paths[key]):
                        # Git does not have the object. Asking again on every query would
                        # cost a lookup per missing blob; a working-copy file is one read.
                        self._deferred[key] = time.monotonic()
                if not prepared:
                    continue
                with transaction(self.conn):
                    for key, kind, size, chunks in prepared:
                        self._store(key, kind, size, chunks)
                        if chunks:
                            indexed += 1
                        else:
                            skipped += 1
                left -= len(prepared)
                stored.update(item[0] for item in prepared)
        if changed or indexed or skipped or frozenset(missing) != self._missing:
            self._refresh_view_chunks()  # also when another session indexed or collected something
        self._missing = frozenset(missing) - stored
        self._pending = left
        self._touch()
        self._fingerprint = view.fingerprint
        return SyncReport(indexed, skipped, left, time.monotonic() - started)

    def serves(self, view: View) -> bool:
        """True if the last sync was for this view, so queries answer for it."""
        return self._fingerprint is not None and self._fingerprint == view.fingerprint

    def paths_for(self, oid: str, family: str) -> list[str]:
        """Paths of the synced view that hold this content under this treatment."""
        return self._paths.get((oid, family), [])

    def path_tokens(self, view: View) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
        """For each path of the synced view: (all its lower-cased words, the words of its file name)."""
        self._require(view)
        if self._path_tokens is None:
            self._path_tokens = {
                path: (frozenset(lexical.query_terms(path)), frozenset(lexical.query_terms(PurePosixPath(path).stem)))
                for path in view.files
            }
        return self._path_tokens

    def narrow(self, view: View, kind: str, paths: Callable[[str], bool] | None, key: object) -> None:
        """Fill temp.scope_chunks with the view's chunks of `kind` ('any', 'code', 'docs')
        in files whose path `paths` accepts. `key` names the filter, so repeating a
        query does not rebuild the table."""
        self._require(view)
        if self._scope == (self._view_epoch, kind, key):
            return
        of_kind = {"any": "", "code": " WHERE c.kind != 'doc'", "docs": " WHERE c.kind = 'doc'"}[kind]
        conn = self.conn
        conn.execute("BEGIN")
        conn.execute("DELETE FROM temp.scope_chunks")
        if paths is None:
            conn.execute(
                "INSERT INTO temp.scope_chunks(chunk_id) SELECT v.chunk_id FROM temp.view_chunks v "
                "JOIN chunks c ON c.chunk_id = v.chunk_id" + of_kind
            )
        else:
            conn.execute("DELETE FROM temp.scope_blobs")
            conn.executemany(
                "INSERT OR IGNORE INTO temp.scope_blobs(blob_oid, family) VALUES (?, ?)",
                (blob for blob, held in self._paths.items() if any(paths(path) for path in held)),
            )
            conn.execute(
                "INSERT INTO temp.scope_chunks(chunk_id) SELECT DISTINCT bc.chunk_id FROM blob_chunks bc "
                "JOIN temp.scope_blobs s ON s.blob_oid = bc.blob_oid AND s.family = bc.family "
                "JOIN chunks c ON c.chunk_id = bc.chunk_id" + of_kind
            )
        conn.execute("COMMIT")
        self._scope = (self._view_epoch, kind, key)

    def counts(self) -> dict[str, int]:
        one = lambda sql: self.conn.execute(sql).fetchall()[0][0]  # noqa: E731
        return {
            "blobs_in_view": one("SELECT count(*) FROM temp.view_blobs"),
            "chunks_in_view": one("SELECT count(*) FROM temp.view_chunks"),
            "blobs_total": one("SELECT count(*) FROM blobs"),
            "chunks_total": one("SELECT count(*) FROM chunks"),
            "pending": self._pending,
        }

    def _require(self, view: View) -> None:
        if not self.serves(view):
            raise ValueError("this view is not the one the index was last synced to; call sync(view) first")

    def _load_view_blobs(self, view: View) -> None:
        self._path_tokens = None
        self._deferred.clear()
        paths: dict[Key, list[str]] = {}
        for path in sorted(view.files):
            paths.setdefault((view.files[path], filters.family(path)), []).append(path)
        self._paths = paths
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM temp.view_blobs")
        self.conn.executemany("INSERT INTO temp.view_blobs(blob_oid, family) VALUES (?, ?)", paths)
        self.conn.execute("COMMIT")

    def _refresh_view_chunks(self) -> None:
        self._view_epoch += 1
        self.conn.execute("BEGIN")
        self.conn.execute("DELETE FROM temp.view_chunks")
        self.conn.execute(
            "INSERT INTO temp.view_chunks(chunk_id) SELECT DISTINCT bc.chunk_id FROM blob_chunks bc "
            "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid AND v.family = bc.family"
        )
        self.conn.execute("COMMIT")

    def _touch(self) -> None:
        """Mark the view's blobs as recently seen, at most once an hour each, so a
        query does not take the write lock every time. The check itself is one index probe."""
        now = int(time.time())
        stale = self.conn.execute(
            "SELECT 1 FROM blobs b JOIN temp.view_blobs v ON v.blob_oid = b.blob_oid AND v.family = b.family "
            "WHERE b.last_seen_at < ? LIMIT 1", (now - SEEN_REFRESH_SECONDS,),
        ).fetchall()
        if stale:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE blobs SET last_seen_at = ? WHERE last_seen_at < ? "
                    "AND (blob_oid, family) IN (SELECT blob_oid, family FROM temp.view_blobs)",
                    (now, now - SEEN_REFRESH_SECONDS),
                )

    def _prepare(self, view: View, reader: gitio.BlobReader, key: Key) -> tuple[Key, str, int, list[Chunk]] | None:
        """What to store for a blob, or None if its content cannot be read right now.
        The index is keyed by content, so nothing may be recorded without the content."""
        _, family = key
        for path in self._paths[key]:
            data = view.read(path, reader)
            if data is not None:
                break
        else:
            return None
        kind, text = filters.classify(family, data)
        if text is None:
            return key, kind, len(data), []
        chunks = chunk_file(family, kind, text)
        return key, (kind if chunks else "skip:empty"), len(data), chunks

    def _store(self, key: Key, kind: str, size: int, chunks: list[Chunk]) -> None:
        conn = self.conn
        oid, family = key
        conn.execute("DELETE FROM blobs WHERE blob_oid = ? AND family = ? AND chunker_version != ?",
                     (oid, family, CHUNKER_VERSION))
        conn.execute(
            "INSERT OR IGNORE INTO blobs(blob_oid, family, kind, size, chunker_version, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (oid, family, kind, size, CHUNKER_VERSION, int(time.time())),
        )
        if conn.changes() == 0:
            return  # another session indexed this blob first
        for ordinal, chunk in enumerate(chunks):
            digest = chunk_hash(family, chunk)
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
                "INSERT INTO blob_chunks(blob_oid, family, ord, chunk_id, start_line, end_line) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (oid, family, ordinal, chunk_id, chunk.start_line, chunk.end_line),
            )


def collect_garbage(conn: apsw.Connection, max_age_days: float, now: float | None = None) -> dict[str, int]:
    """Drop blobs no view has used for `max_age_days`, then the chunks nothing refers to.
    Blobs of the connection's current view are always kept."""
    cutoff = int((time.time() if now is None else now) - max_age_days * 86400)
    with transaction(conn):
        conn.execute(
            "DELETE FROM blobs WHERE last_seen_at < ? "
            "AND (blob_oid, family) NOT IN (SELECT blob_oid, family FROM temp.view_blobs)",
            (cutoff,),
        )
        blobs = conn.changes()
        orphans = [row[0] for row in conn.execute(
            "SELECT chunk_id FROM chunks WHERE chunk_id NOT IN (SELECT chunk_id FROM blob_chunks)")]
        conn.executemany("DELETE FROM chunk_fts WHERE rowid = ?", ((cid,) for cid in orphans))
        conn.executemany("DELETE FROM chunks WHERE chunk_id = ?", ((cid,) for cid in orphans))
    return {"blobs_removed": blobs, "chunks_removed": len(orphans)}
