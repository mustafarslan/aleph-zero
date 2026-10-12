"""Bringing the index up to date with a view, and telling queries which chunks they may return."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import PurePosixPath

import apsw

from . import filters, gitio, lang, lexical
from .chunking import CHUNKER_VERSION, Chunk, chunk_file, chunk_hash, code_chunks
from .lang.definitions import Definition
from .lang.imports import RawImport
from .store import transaction
from .view import View

BATCH = 50
SEEN_REFRESH_SECONDS = 3600
RETRY_SECONDS = 60  # before a blob git could not supply is asked for again under the same view

Key = tuple[str, str]  # (blob id, family): the same bytes are chunked once per treatment


def symbol_key(name: str) -> str:
    """The form a name is looked up by: lower-cased, without a private marker (`#secret`)."""
    return name.lower().lstrip("#")


@contextmanager
def _temp_tables(conn: apsw.Connection):
    """A transaction over the connection's own temporary tables. It takes no lock on the
    index; it only makes the rewrite all-or-nothing and leaves no transaction open on error."""
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@dataclass(frozen=True)
class SyncReport:
    indexed: int    # blobs chunked and stored by this call
    skipped: int    # blobs recorded as not indexable (binary, too large, ...)
    pending: int    # blobs in the view still not indexed: out of time, or not readable right now
    seconds: float


@dataclass(frozen=True)
class _Prepared:
    """Everything to store for one blob under one family."""
    key: Key
    kind: str
    size: int
    chunks: list[Chunk] = field(default_factory=list)
    definitions: list[Definition] = field(default_factory=list)
    chunk_of: list[int] = field(default_factory=list)
    imports: list[RawImport] = field(default_factory=list)


class Index:
    """One connection to a repository's index, and the view it is currently answering for."""

    def __init__(self, conn: apsw.Connection) -> None:
        self.conn = conn
        self._fingerprint: str | None = None
        self._pending = 0
        self._missing: frozenset[Key] = frozenset()  # unindexed blobs of the view at the last sync
        self._deferred: dict[Key, float] = {}  # blob git does not have -> when we last asked
        self._paths: dict[Key, list[str]] = {}
        self._path_words: tuple[dict[str, list[str]], dict[str, list[str]]] | None = None
        self._path_names: frozenset[str] = frozenset()
        self._store_seconds = 0.002  # what storing one blob has cost lately; sizes the last batch before a deadline
        self._view_epoch = 0  # goes up whenever temp.view_chunks is rebuilt
        self._scope: tuple | None = None

    def sync(self, view: View, budget_seconds: float = 5.0,
             stop: Callable[[], bool] | None = None) -> SyncReport:
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
        with gitio.Readers() as reader:
            offset = 0
            size = 4  # small batches first, so the cost of storing is known before a large one is committed to
            while offset < len(todo):
                if time.monotonic() >= deadline or (stop is not None and stop()):
                    break
                batch = todo[offset:offset + size]
                offset += len(batch)
                size = min(BATCH, size * 2)
                prepared = []
                for key in batch:
                    # Stop while there is still time to store what is already prepared.
                    if time.monotonic() + len(prepared) * self._store_seconds >= deadline or (
                            stop is not None and stop()):
                        break
                    item = self._prepare(view, reader, key)
                    if item is not None:
                        prepared.append(item)
                    elif not any(path in view.dirty for path in self._paths[key]):
                        # Git does not have the object. Asking again on every query would
                        # cost a lookup per missing blob; a working-copy file is one read.
                        self._deferred[key] = time.monotonic()
                if not prepared:
                    continue
                storing = time.monotonic()
                with transaction(self.conn):
                    for item in prepared:
                        if not self._store(item):
                            continue  # another session stored it first
                        if item.chunks:
                            indexed += 1
                        else:
                            skipped += 1
                per_blob = (time.monotonic() - storing) / len(prepared)
                self._store_seconds = max(per_blob, 0.5 * self._store_seconds)  # remembers large files for a while
                left -= len(prepared)
                stored.update(item.key for item in prepared)
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

    def path_words(self, view: View) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """For the synced view, two maps from a lower-cased word to the paths it occurs in:
        anywhere in the path, and in the file's own name. Built once per view, so matching
        a query against paths reads only the query's words, not every path."""
        self._require(view)
        if self._path_words is None:
            anywhere: dict[str, list[str]] = {}
            in_name: dict[str, list[str]] = {}
            for path in view.files:
                for word in set(lexical.query_terms(path)):
                    anywhere.setdefault(word, []).append(path)
                for word in set(lexical.query_terms(PurePosixPath(path).stem)):
                    in_name.setdefault(word, []).append(path)
            self._path_words = (anywhere, in_name)
        return self._path_words

    def narrow(self, view: View, kind: str, paths: Callable[[str], bool] | None, key: object) -> None:
        """Fill temp.scope_chunks with the view's chunks of `kind` ('any', 'code', 'docs')
        in files whose path `paths` accepts. `key` names the filter, so repeating a
        query does not rebuild the table."""
        self._require(view)
        if self._scope == (self._view_epoch, kind, key):
            return
        of_kind = {"any": "", "code": " WHERE c.kind != 'doc'", "docs": " WHERE c.kind = 'doc'"}[kind]
        conn = self.conn
        self._scope = None
        with _temp_tables(conn):
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
        self._scope = (self._view_epoch, kind, key)

    def languages(self) -> dict[str, float]:
        """The share of each programming language in the view's indexed source, by size.
        Documentation, data files and anything without a language profile are left out."""
        sizes = dict(self.conn.execute(
            "SELECT b.family, sum(b.size) FROM blobs b JOIN temp.view_blobs v "
            "ON v.blob_oid = b.blob_oid AND v.family = b.family WHERE b.kind NOT LIKE 'skip:%' GROUP BY b.family"))
        code: dict[str, int] = {}
        for family, size in sizes.items():
            if family in lang.PROFILES:
                name = "typescript" if family == "tsx" else family
                code[name] = code.get(name, 0) + size
        total = sum(code.values())
        return {name: size / total for name, size in sorted(code.items(), key=lambda item: -item[1])} if total else {}

    def counts(self) -> dict[str, int]:
        one = lambda sql: self.conn.execute(sql).fetchall()[0][0]  # noqa: E731
        return {
            "blobs_in_view": one("SELECT count(*) FROM temp.view_blobs"),
            "chunks_in_view": one("SELECT count(*) FROM temp.view_chunks"),
            "symbols_in_view": one("SELECT count(*) FROM symbols s JOIN temp.view_blobs v "
                                   "ON v.blob_oid = s.blob_oid AND v.family = s.family"),
            "imports_in_view": one("SELECT count(*) FROM imports i JOIN temp.view_blobs v "
                                   "ON v.blob_oid = i.blob_oid AND v.family = i.family"),
            "blobs_total": one("SELECT count(*) FROM blobs"),
            "chunks_total": one("SELECT count(*) FROM chunks"),
            "pending": self._pending,
        }

    def _require(self, view: View) -> None:
        if not self.serves(view):
            raise ValueError("this view is not the one the index was last synced to; call sync(view) first")

    def _load_view_blobs(self, view: View) -> None:
        if view.files.keys() != self._path_names:  # most edits change content, not paths
            self._path_words = None
            self._path_names = frozenset(view.files)
        self._deferred.clear()
        paths: dict[Key, list[str]] = {}
        for path in sorted(view.files):
            paths.setdefault((view.files[path], filters.family(path)), []).append(path)
        self._paths = paths
        with _temp_tables(self.conn):
            self.conn.execute("DELETE FROM temp.view_blobs")
            self.conn.executemany("INSERT INTO temp.view_blobs(blob_oid, family) VALUES (?, ?)", paths)

    def _refresh_view_chunks(self) -> None:
        self._view_epoch += 1
        with _temp_tables(self.conn):
            self.conn.execute("DELETE FROM temp.view_chunks")
            self.conn.execute(
                "INSERT INTO temp.view_chunks(chunk_id) SELECT DISTINCT bc.chunk_id FROM blob_chunks bc "
                "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid AND v.family = bc.family"
            )

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

    def _prepare(self, view: View, reader: gitio.Readers, key: Key) -> _Prepared | None:
        """What to store for a blob, or None if its content cannot be read right now.
        The index is keyed by content, so nothing may be recorded without the content."""
        _, family = key
        for path in self._paths[key]:
            data = view.read(path, reader, filters.MAX_BYTES)
            if data is not None:
                break
        else:
            return None
        if isinstance(data, gitio.TooLarge):
            return _Prepared(key, "skip:too-large", data.size)
        kind, text = filters.classify(family, data)
        if text is None:
            return _Prepared(key, kind, len(data))
        profile = lang.PROFILES.get(family)
        if profile is not None:
            code = code_chunks(profile, text)
            if code.chunks:
                return _Prepared(key, kind, len(data), code.chunks, code.definitions, code.chunk_of, code.imports)
        chunks = chunk_file(family, kind, text)
        return _Prepared(key, kind if chunks else "skip:empty", len(data), chunks)

    def _store(self, item: _Prepared) -> bool:
        """Write one blob. False if another session had already written it."""
        conn = self.conn
        oid, family = item.key
        conn.execute("DELETE FROM blobs WHERE blob_oid = ? AND family = ? AND chunker_version != ?",
                     (oid, family, CHUNKER_VERSION))
        conn.execute(
            "INSERT OR IGNORE INTO blobs(blob_oid, family, kind, size, chunker_version, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (oid, family, item.kind, item.size, CHUNKER_VERSION, int(time.time())),
        )
        if conn.changes() == 0:
            return False
        for ordinal, chunk in enumerate(item.chunks):
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
                "INSERT INTO blob_chunks(blob_oid, family, ord, chunk_id, start_line, end_line, scope_start, scope_end) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (oid, family, ordinal, chunk_id, chunk.start_line, chunk.end_line,
                 chunk.scope_start_line, chunk.scope_end_line),
            )
        conn.executemany(
            "INSERT INTO symbols(blob_oid, family, ord, name, name_key, qualified, kind, signature, chunk_ord, line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((oid, family, ordinal, d.name, symbol_key(d.name), d.qualified, d.kind, d.signature, chunk_ord, d.start_line)
             for ordinal, (d, chunk_ord) in enumerate(zip(item.definitions, item.chunk_of))),
        )
        conn.executemany(
            "INSERT INTO imports(blob_oid, family, ord, specifier, names, level, kind, flags, line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ((oid, family, ordinal, i.specifier, json.dumps(list(i.names)), i.level, i.kind,
              (1 if i.reexport else 0) | (2 if i.deferred else 0), i.line)
             for ordinal, i in enumerate(item.imports)),
        )
        return True


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
