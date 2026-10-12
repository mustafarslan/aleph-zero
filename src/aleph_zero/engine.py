"""What lasts longer than one query: an index connection per worktree, time limits, and
what this session has already been shown.

The command line builds an engine for one call; the MCP server keeps one for the
session. Every operation takes the directory it is asked about, so one engine serves
any number of worktrees and repositories.
"""
from __future__ import annotations

import functools
import hashlib
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import apsw

from . import dense_policy, gitio, lang, render, sources, symbols
from . import outline as outlines
from .chunking import CHUNKER_VERSION
from .indexer import Index, collect_if_due
from .search import search as run_search
from .store import DAMAGED, SCHEMA_VERSION, index_file, index_note, open_db
from .store import db as store_db
from .view import View, build_view

SYNC_SECONDS = 5.0       # how long a query without a time limit may spend indexing first
SEARCH_SHARE = 0.25      # of a time limit, kept for the search itself
SHOWN_LIMIT = 5000       # texts remembered as shown before the memory starts over
# How long a query waits for its own vector. Awake, the worker answers in 6 to 20 ms. Asleep it
# takes half a second to wake: that query is answered by words and names, and the next one finds it awake.
QUERY_EMBED_SECONDS = 0.3
_LANGUAGES = {
    "py": ("python",), "ts": ("typescript", "tsx"), "typescript": ("typescript", "tsx"), "js": ("javascript",),
    "golang": ("go",), "rs": ("rust",), "c++": ("cpp",), "md": ("markdown",), "rst": ("prose",),
}


@dataclass(frozen=True)
class SymbolAnswer:
    name: str
    symbols: list[symbols.SymbolHit] = field(default_factory=list)
    pending: int = 0
    timed_out: bool = False
    elapsed_ms: int = 0


class Cancelled(Exception):
    """The caller withdrew the request while it was running."""


@contextmanager
def deadline(conn: apsw.Connection, until: float | None, stop: threading.Event | None = None):
    """Inside this block SQLite stops with apsw.InterruptError once `until` has passed
    or `stop` is set."""
    if until is None and stop is None:
        yield
        return
    conn.set_progress_handler(
        lambda: (until is not None and time.monotonic() > until) or (stop is not None and stop.is_set()), 2000)
    try:
        yield
    finally:
        conn.set_progress_handler(None)


def _rebuilding(method):
    """Run an operation; if the index file turns out to be damaged, set it aside, start a
    new index and run the operation once more. The index is a cache of what git holds."""
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except DAMAGED:
            self._discard_damaged()
            return method(self, *args, **kwargs)
    return call


class Engine:
    def __init__(self, default_cwd: Path | None = None, dense: bool | None = None) -> None:
        self.default_cwd = Path(default_cwd) if default_cwd is not None else Path.cwd()
        # Semantic search: True or False is the owner's choice; None follows the benchmark
        # verdict for each worktree's main language (dense_policy).
        self.dense = dense
        self._stores: dict[Path, object] = {}
        self._decisions: dict[int, tuple] = {}
        self._embedder = None  # the connection to the worker used for queries
        self._indexes: dict[Path, Index] = {}   # one connection per worktree: each holds one view
        self._files: dict[Path, tuple[Path, int]] = {}  # per worktree: the index file and its inode when opened
        self._last: Path | None = None          # the worktree the running operation uses
        self._shown: set[tuple] = set()
        self._stop = threading.Event()

    def cancel(self) -> None:
        """Stop the operation running on another thread; it raises Cancelled."""
        self._stop.set()

    def resume(self) -> None:
        """Forget a cancellation, before the next operation."""
        self._stop.clear()

    def close(self) -> None:
        """Close every index connection. The engine can be used again; it reopens what it needs."""
        for root in list(self._indexes):
            self._forget(root)

    def _forget(self, root: Path) -> None:
        index = self._indexes.pop(root, None)
        self._stores.pop(root, None)
        self._files.pop(root, None)
        if index is not None:
            self._decisions.pop(id(index), None)
            try:
                index.conn.close()
            except apsw.Error:
                pass

    def _discard_damaged(self) -> None:
        """The index file used by the running operation is damaged: drop every connection
        to it and move it out of the way, unless another session already has."""
        if self._last is None or self._last not in self._files:
            raise
        path, opened = self._files[self._last]
        for root in [root for root, (file, _) in self._files.items() if file == path]:
            self._forget(root)
        store_db.set_aside(path, opened)

    # ---- the four operations --------------------------------------------------------------

    @_rebuilding
    def search(self, query: str, k: int = 8, kind: str = "any", path_glob: str | None = None,
               lang: str | None = None, cwd: Path | str | None = None,
               timeout_ms: int | None = None) -> render.Answer:
        families = _families(lang)
        started, until = _limits(timeout_ms)

        def read(index: Index, view: View):
            note, covered, chunks, nearest = self._dense(index, view, query)
            return note, covered, chunks, lambda: run_search(
                index, view, query, k=k, kind=kind, path_glob=path_glob, families=families, dense=nearest) or []

        try:
            (note, covered, chunks, hits), pending, late = self._consistent(cwd, started, until, read, ("", 0, 0))
        except gitio.GitTimeout:
            return render.Answer(query, [], 0, True, _elapsed(started))  # not even the worktree was read in time
        return render.Answer(query, hits, pending, late, _elapsed(started), note, covered, chunks)

    @_rebuilding
    def embed(self, cwd: Path | str | None = None, budget_seconds: float = float("inf"), batch: int | None = None,
              stop=None, progress=None, sync_seconds: float = SYNC_SECONDS):
        """Give the chunks of this worktree their vectors (slow: about a hundred a second).
        Starts the worker if needed. Returns an embedder.EmbedReport."""
        from . import embedder
        from .embed import worker
        from .embed.client import EmbedPool, Unavailable

        index, view, _, _ = self._ready(cwd, time.monotonic(), None, sync_seconds=sync_seconds)
        store = self._store(index, view)
        try:
            sock = worker.ensure()
        except Unavailable as exc:
            have, total = store.coverage(index)
            return embedder.EmbedReport(0, total - have, 0.0, str(exc))
        pool = EmbedPool(sock, capacity=worker.capacity_tokens(), connections=worker.slots())
        try:
            return embedder.embed_pending(index, store, pool, budget_seconds, batch or embedder.BATCH * worker.slots(),
                                          stop, progress)
        finally:
            pool.close()

    @_rebuilding
    def symbol(self, name: str, k: int = 20, cwd: Path | str | None = None,
               timeout_ms: int | None = None) -> SymbolAnswer:
        started, until = _limits(timeout_ms)
        try:
            (found,), pending, late = self._consistent(
                cwd, started, until, lambda index, view: (lambda: symbols.lookup(index, view, name, limit=k),), ())
        except gitio.GitTimeout:
            return SymbolAnswer(name, [], 0, True, _elapsed(started))
        return SymbolAnswer(name, found, pending, late, _elapsed(started))

    def outline(self, path: str, cwd: Path | str | None = None,
                timeout_ms: int | None = None) -> outlines.Outline:
        """Raises gitio.GitTimeout when the worktree cannot be read within the limit."""
        with gitio.until(_limits(timeout_ms)[1]):
            return outlines.of(self._view(cwd), path)

    @_rebuilding
    def status(self, cwd: Path | str | None = None, timeout_ms: int | None = None) -> dict:
        """Never indexes. Raises gitio.GitTimeout when the worktree cannot be read within the limit."""
        with gitio.until(_limits(timeout_ms)[1]):
            view = self._view(cwd)
        index = self._index(view)
        pending = index.sync(view, budget_seconds=0.0).pending
        counts = index.counts()
        shares = index.languages()
        try:
            linked, problem = sources.describe(view.common_dir), ""
        except sources.SourceError as exc:
            linked, problem = [], str(exc)
        return {
            "root": str(view.root), "files_in_view": len(view.files), "dirty": len(view.dirty), "pending": pending,
            "chunks_in_view": counts["chunks_in_view"], "symbols_in_view": counts["symbols_in_view"],
            "imports_in_view": counts["imports_in_view"], "index": str(index_file(view.common_dir)),
            "index_note": index_note(view.common_dir),
            "lost_to_collection": index.lost_to_collection,
            "unreadable": index.unreadable,
            "outside_checkout": view.outside_checkout,
            "submodules": list(view.submodules),
            **({"submodules_note": "submodules are not searched; link one as a source with "
                                   f"`aleph source add {view.submodules[0]}`"} if view.submodules else {}),
            "schema_version": SCHEMA_VERSION, "chunker_version": CHUNKER_VERSION,
            "sqlite": apsw.sqlite_lib_version(), "untracked_cache": gitio.untracked_cache_enabled(view.root),
            "sources": linked, **({"sources_error": problem} if problem else {}),
            "main_language": next(iter(shares), None),
            "language_share": {name: round(share, 3) for name, share in shares.items()},
            "vectors": self._vector_status(index, view),
        }

    # ---- text for an agent, with the session's memory ----------------------------------------

    def render(self, answer: render.Answer, detail: str = "snippets",
               budget_tokens: int = render.DEFAULT_BUDGET) -> str:
        """The answer as text. Text this session has already been shown is replaced by a
        pointer, unless `detail` is 'full'."""
        if len(self._shown) > SHOWN_LIMIT:
            self._shown.clear()
        return render.text(answer, budget_tokens, detail, already_shown=lambda hit: _key(hit) in self._shown,
                           printed=lambda hit: self._shown.add(_key(hit)))

    # ---- views and indexes ------------------------------------------------------------------------

    def _view(self, cwd: Path | str | None) -> View:
        return build_view(Path(cwd) if cwd is not None else self.default_cwd)

    def _store(self, index: Index, view: View):
        from .embed.client import VECTOR_KIND
        from .vectors import VectorStore  # numpy is loaded only when vectors are used

        store = self._stores.get(view.root)
        if store is None:
            store = self._stores[view.root] = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
        return store

    def _vector_status(self, index: Index, view: View) -> dict:
        from .embed.client import VECTOR_KIND

        store = self._store(index, view)
        have, total = store.coverage(index)
        enabled, why = self._dense_decision(index)
        return {"enabled": enabled, "why": why, "kind": VECTOR_KIND, "with_vector": have, "chunks": total,
                **store.rows()}

    def _dense_decision(self, index: Index) -> tuple[bool, str]:
        """Whether vectors take part for the worktree this index is synced to, and why."""
        if self.dense is not None:
            return dense_policy.decide(self.dense, None)
        key = index._view_epoch
        cached = self._decisions.get(id(index))
        if cached is None or cached[0] != key:
            enabled, why = dense_policy.decide(None, next(iter(index.languages()), None))
            if enabled:
                from .embed import setup

                if not setup.installed():  # the verdict is in favour, but there is nothing to make vectors with
                    enabled, why = False, why + "; the embedding worker is not installed (`aleph embed setup`)"
            cached = (key, (enabled, why))
            self._decisions[id(index)] = cached
        return cached[1]

    def dense_enabled(self, cwd: Path | str | None = None) -> bool:
        """Whether vectors take part for the worktree containing `cwd`. Does not index."""
        index, _, _, _ = self._ready(cwd, time.monotonic(), None, sync_seconds=0.0)
        return self._dense_decision(index)[0]

    def _dense(self, index: Index, view: View, query: str):
        """How vectors take part in this query: (note, chunks covered, chunks, the function
        that ranks by vector or None). Nothing here may fail or delay the query."""
        if not self._dense_decision(index)[0]:
            return "", 0, 0, None
        from .embed import worker
        from .embed.client import EmbedClient, Unavailable

        store = self._store(index, view)
        covered, chunks = store.coverage(index)
        if covered == 0:
            return "no chunk of this worktree has a vector yet (`aleph embed run` makes them)", 0, chunks, None
        sock = worker.socket_path()
        if not sock.exists():  # no probe here: a hung worker would hold the query for the probe's timeout
            return "the embedding worker is not running", covered, chunks, None
        if self._embedder is None or self._embedder.socket_path != sock:
            self._embedder = EmbedClient(sock, capacity=worker.capacity_tokens(), timeout=QUERY_EMBED_SECONDS)
        try:
            vector = self._embedder.embed([query], kind="query")[0]
        except Unavailable as exc:
            return str(exc), covered, chunks, None
        return "used", covered, chunks, lambda limit, scoped: [chunk_id for chunk_id, _ in
                                                               store.search(index, vector, limit, scoped)]

    def _index(self, view: View) -> Index:
        self._last = view.root
        index = self._indexes.get(view.root)
        if index is None:
            path = index_file(view.common_dir)
            index = self._indexes[view.root] = Index(open_db(path))
            self._files[view.root] = (path, store_db.inode(path))
        return index

    def _ready(self, cwd: Path | str | None, started: float, until: float | None,
               sync_seconds: float = SYNC_SECONDS) -> tuple[Index, View, int, bool]:
        """The index of the worktree containing `cwd`, synced to its current view within
        the time allowed. Returns (index, view, files still pending, out of time)."""
        with gitio.until(until):
            view = self._view(cwd)
        index = self._index(view)
        if until is None:
            budget = sync_seconds
        else:
            budget = max(0.0, (until - time.monotonic()) * (1 - SEARCH_SHARE))
        report = index.sync(view, budget_seconds=budget, stop=self._stop.is_set)
        if self._stop.is_set():
            raise Cancelled
        if until is None and (report.indexed or report.skipped):
            # New content was stored, so old content may be due to go. Not under a caller's
            # time limit, and never an error for the query that happens to trigger it.
            try:
                collect_if_due(index.conn)
            except apsw.BusyError:
                pass
        return index, view, report.pending, until is not None and report.pending > 0


    def _consistent(self, cwd: Path | str | None, started: float, until: float | None, prepare, empty: tuple):
        """Answer from one state of the index that holds the worktree's view.

        `prepare(index, view)` returns a tuple whose last item is the function that reads
        the index; the answer is that tuple with the function's result in its place.
        Returns (answer, files still pending, out of time).

        Other sessions index and collect while this one answers. Between finding the index
        complete and reading it, a collection can remove what was found. So the read runs
        in one transaction, and counts only if nothing was committed around it or, looking
        again inside that transaction, the view is still all there. Otherwise the index is
        synced again. On the last attempt an incomplete state is answered from and said so."""
        attempts = 3
        for attempt in range(attempts):
            index, view, pending, late = self._ready(cwd, started, until)
            *before, read = prepare(index, view)
            result = empty[len(before):] if empty else ()
            if late and time.monotonic() >= until:
                return (*before, []), pending, late
            quiet = attempt == 0 and index.undisturbed()  # nothing committed since the sync looked
            sound = True
            try:
                with index.reading():
                    if not quiet:
                        sound = index.still_serves()
                    if sound or attempt + 1 == attempts:
                        with deadline(index.conn, until, self._stop):
                            found = read()
                    else:
                        continue
            except apsw.InterruptError:
                _reset(index.conn)
                if self._stop.is_set():
                    raise Cancelled
                return (*before, []), pending, True
            if self._stop.is_set():
                raise Cancelled
            if quiet and not index.undisturbed() and attempt + 1 < attempts:
                continue  # a commit landed while reading: the transaction may have begun after it
            if not sound:
                pending = max(pending, 1)  # answered from a state that lacks part of the view
            return (*before, found), pending, late
        raise AssertionError("unreachable")

def _limits(timeout_ms: int | None) -> tuple[float, float | None]:
    started = time.monotonic()
    return started, (None if timeout_ms is None else started + max(timeout_ms, 1) / 1000)


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _reset(conn: apsw.Connection) -> None:
    """After an interrupted statement: leave no transaction open on the connection."""
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def _families(name: str | None) -> frozenset[str] | None:
    if name is None:
        return None
    key = name.strip().lower()
    if key in _LANGUAGES:
        return frozenset(_LANGUAGES[key])
    if key in lang.PROFILES or key in ("markdown", "prose", "text"):
        return frozenset({key})
    known = sorted(set(lang.PROFILES) | set(_LANGUAGES) | {"markdown", "prose", "text"})
    raise ValueError(f"unknown language {name!r}; one of: {', '.join(known)}")


def _key(hit) -> tuple:
    return (hit.path, hit.start_line, hit.end_line, hashlib.blake2b(hit.text.encode("utf-8", "replace"), digest_size=8).digest())
