"""Bringing the index up to date with a view, and telling queries which chunks they may return."""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import apsw

from . import filters, gitio, lang, lexical
from .chunking import CHUNKER_VERSION, Chunk, chunk_file, chunk_hash, code_chunks
from .lang.definitions import Definition
from .lang.languages import stem_of
from .lang.imports import RawImport
from .store import StoreError, transaction
from .view import View

BATCH = 50
SEEN_REFRESH_SECONDS = 3600


def seen_refresh_seconds() -> int:
    """How stale `last_seen_at` may get for content in use. Garbage collection must use an
    age well above this. The setting exists for the stress run, which shortens both."""
    return int(float(os.environ.get("ALEPH_SEEN_REFRESH_SECONDS", SEEN_REFRESH_SECONDS)))
RETRY_SECONDS = 60  # before a blob git could not supply is asked for again under the same view

Key = tuple[str, str]  # (blob id, family): the same bytes are chunked once per treatment


def symbol_key(name: str) -> str:
    """The form a name is looked up by: lower-cased, without a private marker (`#secret`)."""
    return name.lower().lstrip("#")


@contextmanager
def _temp_tables(conn: apsw.Connection):
    """A rewrite of the connection's own temporary tables, all or nothing. It takes no lock
    on the index. A savepoint, so it also works inside `Index.reading()`."""
    conn.execute("SAVEPOINT temp_tables")
    try:
        yield
    except BaseException:
        # When SQLite interrupts a write inside a transaction (a query out of time) it rolls the whole
        # transaction back by itself, and the savepoint is gone with it: undoing it again is then an error of
        # its own, and that error, not the interruption, would go up. What matters is the first one.
        try:
            conn.execute("ROLLBACK TO temp_tables")
            conn.execute("RELEASE temp_tables")
        except apsw.Error:
            pass
        raise
    conn.execute("RELEASE temp_tables")


_MISSING = ("SELECT v.blob_oid, v.family FROM temp.view_blobs v LEFT JOIN blobs b "
            "ON b.blob_oid = v.blob_oid AND b.family = v.family AND b.chunker_version = ? "
            "WHERE b.blob_oid IS NULL ORDER BY v.blob_oid, v.family")


def collections(conn: apsw.Connection) -> int:
    """How many times content has been removed from this index."""
    found = conn.execute("SELECT value FROM meta WHERE key = 'collections'").fetchall()
    return found[0][0] if found else 0


def _removed_content(conn: apsw.Connection) -> None:
    """Inside a write transaction that deleted blobs or chunks: let every session know."""
    conn.execute("INSERT INTO meta(key, value) VALUES ('collections', 1) "
                 "ON CONFLICT(key) DO UPDATE SET value = value + 1")


@dataclass(frozen=True)
class SyncReport:
    indexed: int    # blobs chunked and stored by this call
    skipped: int    # blobs recorded as not indexable (binary, too large, ...)
    pending: int    # blobs in the view still not indexed: out of time, or a working-copy file that changed under us
    seconds: float
    unreadable: int = 0  # blobs git does not have (a partial clone, a damaged store): not waited for


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
        self._unreadable: frozenset[Key] = frozenset()  # those of them still missing after the last sync
        self._paths: dict[Key, list[str]] = {}
        self._path_words: tuple[dict[str, list[str]], dict[str, list[str]]] | None = None
        self._path_names: frozenset[str] = frozenset()
        self._paths_asked = False       # whether these paths have been asked one question yet
        self._store_seconds = 0.002  # what storing one blob has cost lately; sizes the last batch before a deadline
        self._view_epoch = 0  # goes up whenever temp.view_chunks is rebuilt
        self._scope: tuple | None = None
        self._keys: frozenset[Key] = frozenset()     # every blob of the view
        self._indexed: frozenset[Key] = frozenset()  # those that were in the index at the last sync
        self.lost_to_collection = 0  # blobs of a live view that another session's collection removed
        self.full_checks = 0         # syncs that had to look at the whole view
        self._version: int | None = None   # SQLite's data_version when the last sync looked: other sessions' commits change it
        self._collections = -1       # the index's count of removals when the list of chunks was built
        self._touched_at = 0.0

    def sync(self, view: View, budget_seconds: float = 5.0,
             stop: Callable[[], bool] | None = None) -> SyncReport:
        """Index every blob of the view the index has not seen, within a time budget,
        then point the connection's view tables at this view.

        Cheap when nothing changed, but never skipped: another session may have
        collected this view's content, or indexed what was pending here."""
        started = time.monotonic()
        changed = view.fingerprint != self._fingerprint
        # Read before looking: a commit by another session after this point is seen as a change next time.
        version = self.conn.pragma("data_version")
        if (not changed and version == self._version and self._missing == self._unreadable
                and all(asked > started - RETRY_SECONDS for asked in self._deferred.values())):
            # The same view, nothing pending, and no other session has written: the index is as it was.
            self._touch()
            return SyncReport(0, 0, 0, time.monotonic() - started, len(self._unreadable))
        self.full_checks += 1
        if changed:
            self._fingerprint = None  # until the tables below match the view again
            self._load_view_blobs(view)
        removals = collections(self.conn)
        missing: list[Key] = [(row[0], row[1]) for row in self.conn.execute(_MISSING, (CHUNKER_VERSION,))]
        if missing:
            # Content of the view that a build with a later chunker has read is not this build's to read again:
            # the two would take turns re-indexing the repository, one whole pass for every switch between them.
            newer = self.conn.execute(
                "SELECT max(b.chunker_version) FROM temp.view_blobs v JOIN blobs b ON b.blob_oid = v.blob_oid AND b.family = v.family "
                "WHERE b.chunker_version > ?", (CHUNKER_VERSION,)).fetchall()[0][0]
            if newer is not None:
                raise StoreError(newer_build(self.conn.filename, newer))
        self.lost_to_collection += len(self._indexed.intersection(missing))
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
        if changed or indexed or skipped or frozenset(missing) != self._missing or removals != self._collections:
            # Also when another session indexed something of this view, or content was removed:
            # the same blobs may be back under new chunk ids.
            self._refresh_view_chunks()
            self._collections = removals
        self._missing = frozenset(missing) - stored
        self._indexed = self._keys - self._missing if self._missing else self._keys  # no copy on the usual path
        # What git could not supply is not waited for: it is asked for again in a minute,
        # and counted apart, so answers are not marked incomplete for ever.
        self._unreadable = self._missing.intersection(self._deferred)
        left -= len(self._unreadable)
        self._pending = left
        self._touch(now=True)
        self._fingerprint = view.fingerprint
        self._version = version
        return SyncReport(indexed, skipped, left, time.monotonic() - started, len(self._unreadable))

    def release_memory(self) -> None:
        """Drop what is rebuilt on demand: the words of every path, SQLite's cached pages."""
        self._path_words = None
        self._path_names = frozenset()
        self.conn.pragma("shrink_memory")

    @property
    def unreadable(self) -> int:
        """Blobs of the view that git does not have."""
        return len(self._unreadable)

    @contextmanager
    def reading(self):
        """Everything read inside sees one state of the index, whatever other sessions commit meanwhile."""
        conn = self.conn
        conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._scope = None  # the filter tables may have been half rewritten
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def undisturbed(self) -> bool:
        """No other session has committed since the last sync looked at the index. Not to be
        asked inside `reading()`: SQLite reports the version from before the transaction."""
        return self._version is not None and self.conn.pragma("data_version") == self._version

    def still_serves(self) -> bool:
        """Inside `reading()`: whether the state being read holds everything the last sync
        found for this view, under the chunk ids it listed. Looks at the whole view."""
        if collections(self.conn) != self._collections:
            return False
        return frozenset((row[0], row[1]) for row in self.conn.execute(_MISSING, (CHUNKER_VERSION,))) == self._missing

    def serves(self, view: View) -> bool:
        """True if the last sync was for this view, so queries answer for it."""
        return self._fingerprint is not None and self._fingerprint == view.fingerprint

    def paths_for(self, oid: str, family: str) -> list[str]:
        """Paths of the synced view that hold this content under this treatment."""
        return self._paths.get((oid, family), [])

    def path_words(self, view: View, terms: list[str] | None = None) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """For the synced view, two maps from a lower-cased word to the paths it occurs in:
        anywhere in the path, and in the file's own name. Built once per view, so matching
        a query against paths reads only the query's words, not every path.

        The first question a set of paths is asked (`terms`: its words) is answered without the
        maps, from the paths that contain one of the words at all: a process that asks once and
        exits does not read every path of a large repository into words. The second builds them."""
        self._require(view)
        if self._path_words is None and terms is not None and not self._paths_asked:
            self._paths_asked = True
            wanted = set(terms)
            # A word lower-cased alone ends in a final sigma where the same word inside a longer text does not.
            plain = [term.replace("ς", "σ") for term in wanted]
            return _path_words((path for path in view.files
                                if any(term in path.lower().replace("ς", "σ") for term in plain)), wanted)
        if self._path_words is None:
            self._path_words = _path_words(view.files, None)
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
            self._paths_asked = False
            self._path_names = frozenset(view.files)
        self._deferred.clear()
        paths: dict[Key, list[str]] = {}
        for path in sorted(view.files):
            paths.setdefault((view.files[path], filters.family(path)), []).append(path)
        self._paths = paths
        self._keys = frozenset(paths)
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

    def _touch(self, now: bool = False) -> None:
        """Mark the view's blobs as recently seen, at most once an hour each, so a query does
        not take the write lock every time. Looking for stale ones reads the whole view, so
        that too is done only now and then, unless the sync has just looked anyway."""
        moment = time.time()
        refresh = seen_refresh_seconds()
        if not now and moment - self._touched_at < min(refresh / 4, 300):
            return
        self._touched_at = moment
        stale = self.conn.execute(
            "SELECT 1 FROM blobs b JOIN temp.view_blobs v ON v.blob_oid = b.blob_oid AND v.family = b.family "
            "WHERE b.last_seen_at < ? LIMIT 1", (int(moment) - refresh,),
        ).fetchall()
        if stale:
            with transaction(self.conn):
                self.conn.execute(
                    "UPDATE blobs SET last_seen_at = ? WHERE last_seen_at < ? "
                    "AND (blob_oid, family) IN (SELECT blob_oid, family FROM temp.view_blobs)",
                    (int(moment), int(moment) - refresh),
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
        # Only what an older chunker wrote is replaced. A row of a newer build (written while this sync was
        # already under way, past its check) stays: the insert below then does nothing, and the next sync refuses.
        conn.execute("DELETE FROM blobs WHERE blob_oid = ? AND family = ? AND chunker_version < ?",
                     (oid, family, CHUNKER_VERSION))
        if conn.changes():
            _removed_content(conn)  # another session may be listing this blob's chunks
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


COLLECT_EVERY_SECONDS = 86400   # collection by itself: at most once a day per index
COLLECT_AGE_DAYS = 30.0
COLLECT_SECONDS = 0.5           # what one round may take; it runs after a query's indexing
COLLECT_RETRY_SECONDS = 60      # before the next round when one did not finish
_BLOB_BATCH = 500
_CHUNK_BATCH = 2000


def newer_build(index_file: str, theirs: int) -> str:
    """What an older build says of an index a newer one has written to, and the ways out."""
    return (f"this index holds content read by a newer version of Aleph (its chunker is {theirs}, this one's is "
            f"{CHUNKER_VERSION}). Two versions sharing an index would each redo the other's work on every query: "
            f"upgrade this installation, or use the newer one. To start over with this version instead, delete "
            f"{Path(index_file).parent} (it is a cache: nothing but time is lost)")


def _path_words(paths, wanted: set[str] | None) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(word -> paths it occurs in anywhere, word -> paths whose own name has it), for all words or the wanted ones.

    The words of a path are those `lexical.query_terms` gives for it. They are read by the directory and by the
    file's name, since a repository has far fewer of both than files (babel: 26,000 files, most of them `input.js`
    and `output.js` in a directory of their own; path by path that was 140 ms of a first question). A word is
    cut at no `/`, so a path's words are its directory's and its name's; what `query_terms` does to a whole text
    (stop words left out unless nothing else is there, a limit on the words read) is kept by reading a path by
    itself wherever either could differ."""
    anywhere: dict[str, list[str]] = {}
    in_name: dict[str, list[str]] = {}
    keep = (lambda words: words) if wanted is None else (lambda words: words & wanted)
    short = lexical.MAX_TERMS // 2          # under this in the directory and in the name, no limit is reached
    by_directory: dict[str, list[str]] = {}
    for path in paths:
        by_directory.setdefault(path.rpartition("/")[0], []).append(path)
    names: dict[str, tuple[frozenset[str], frozenset[str]]] = {}      # a name's words: all of them, and without stop words
    by_name: dict[str, list[str]] = {}
    endings: dict[str, frozenset[str]] = {}                           # the words of `.js`, `.test.ts`: a handful in a repository
    for directory, held in by_directory.items():
        forms = lexical.all_forms(directory)
        shared = forms - lexical.STOPWORDS if len(forms) < short else frozenset()
        for word in keep(shared):
            anywhere.setdefault(word, []).extend(held)
        start = len(directory) + 1 if directory else 0
        for path in held:
            name = path[start:]
            known = names.get(name)
            if known is None:
                # The name is read once, as its stem and its ending: the stem's words are wanted by themselves below.
                stem = stem_of(name)
                of_stem = lexical.all_forms(stem)
                ending = endings.get(name[len(stem):])
                if ending is None:
                    ending = endings[name[len(stem):]] = lexical.all_forms(name[len(stem):])
                every = of_stem | ending if ending else of_stem
                known = names[name] = (every, every - lexical.STOPWORDS)
                in_stem = (of_stem - lexical.STOPWORDS or of_stem) if len(of_stem) < short else frozenset(lexical.query_terms(stem))
                by_name[name] = [in_stem]
            by_name[name].append(path)
            if len(forms) >= short or len(known[0]) >= short:
                own = frozenset(lexical.query_terms(path)) - shared
            elif shared:
                own = known[1] - shared
            else:                               # nothing but stop words above it, or nothing at all
                own = known[1] or (forms | known[0])
            for word in keep(own):
                anywhere.setdefault(word, []).append(path)
    for in_stem, *held in by_name.values():
        for word in keep(in_stem):
            in_name.setdefault(word, []).extend(held)
    return anywhere, in_name


def collect_garbage(conn: apsw.Connection, max_age_days: float, now: float | None = None,
                    budget_seconds: float | None = None) -> dict:
    """Drop blobs no view has used for `max_age_days`, then the chunks nothing refers to.
    Blobs of the connection's current view are always kept. Works in short transactions,
    so other sessions are never locked out for long, and stops at the time limit:
    'finished' says whether everything due was removed."""
    cutoff = int((time.time() if now is None else now) - max_age_days * 86400)
    deadline = None if budget_seconds is None else time.monotonic() + budget_seconds
    late = lambda: deadline is not None and time.monotonic() >= deadline  # noqa: E731
    blobs = chunks = 0
    finished = True
    while True:
        if late():
            finished = False
            break
        with transaction(conn):
            batch = conn.execute(
                "SELECT blob_oid, family FROM blobs WHERE last_seen_at < ? "
                "AND (blob_oid, family) NOT IN (SELECT blob_oid, family FROM temp.view_blobs) LIMIT ?",
                (cutoff, _BLOB_BATCH)).fetchall()
            if batch:
                conn.executemany("DELETE FROM blobs WHERE blob_oid = ? AND family = ?", batch)
                _removed_content(conn)
        if not batch:
            break
        blobs += len(batch)
    while finished:
        if late():
            finished = False
            break
        with transaction(conn):  # looked for inside the transaction: a chunk another session has just reused is not one
            orphans = [row[0] for row in conn.execute(
                "SELECT chunk_id FROM chunks WHERE chunk_id NOT IN (SELECT chunk_id FROM blob_chunks) LIMIT ?",
                (_CHUNK_BATCH,))]
            if orphans:
                conn.executemany("DELETE FROM chunk_fts WHERE rowid = ?", ((cid,) for cid in orphans))
                conn.executemany("DELETE FROM chunks WHERE chunk_id = ?", ((cid,) for cid in orphans))
                _removed_content(conn)
        if not orphans:
            break
        chunks += len(orphans)
    return {"blobs_removed": blobs, "chunks_removed": chunks, "finished": finished}


def collect_if_due(conn: apsw.Connection, budget_seconds: float = COLLECT_SECONDS) -> dict | None:
    """Collect garbage if this index has not been collected for a day. Returns the report,
    or None when it was not due or another session has just taken the turn.

    The ages are far apart on purpose: content in use is marked at least every 65 minutes
    (`_touch`), and content is collected after 30 days without a mark."""
    every = float(os.environ.get("ALEPH_GC_EVERY_SECONDS", COLLECT_EVERY_SECONDS))
    age_days = float(os.environ.get("ALEPH_GC_AGE_DAYS", COLLECT_AGE_DAYS))
    now = int(time.time())
    last = conn.execute("SELECT value FROM meta WHERE key = 'collected_at'").fetchall()
    if last and now < last[0][0] + every:
        return None  # the usual answer, without taking the write lock
    with transaction(conn):  # only one session takes the turn
        last = conn.execute("SELECT value FROM meta WHERE key = 'collected_at'").fetchall()
        conn.execute("INSERT INTO meta(key, value) VALUES ('collected_at', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (now,))
        if not last:
            return None  # a new index: nothing in it can be old
        if now < last[0][0] + every:
            return None
    report = collect_garbage(conn, age_days, budget_seconds=budget_seconds)
    if not report["finished"]:
        with transaction(conn):  # come back soon for the rest
            conn.execute("UPDATE meta SET value = ? WHERE key = 'collected_at'",
                         (int(now - every + COLLECT_RETRY_SECONDS),))
    return report
