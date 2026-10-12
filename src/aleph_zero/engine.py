"""What lasts longer than one query: an index connection per worktree, time limits, and
what this session has already been shown.

The command line builds an engine for one call; the MCP server keeps one for the
session. Every operation takes the directory it is asked about, so one engine serves
any number of worktrees and repositories.
"""
from __future__ import annotations

import functools
import gc
import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import apsw

from . import callgraph, dense_policy, gitio, indexers, lang, memory, memstat, render, scipstore, sources, symbols
from . import view as views
from . import outline as outlines
from .chunking import CHUNKER_VERSION
from .indexer import Index, collect_if_due
from .lang.parse import forget_parsers
from .search import search as run_search
from .store import DAMAGED, SCHEMA_VERSION, index_file, index_note, open_db, state_dir
from .store import db as store_db
from .view import View, build_view

SYNC_SECONDS = 5.0       # how long a query without a time limit may spend indexing first
SEARCH_SHARE = 0.25      # of a time limit, kept for the search itself
SHOWN_LIMIT = 5000       # texts remembered as shown before the memory starts over
GRAPH_SECONDS = 60.0     # how long a graph question without a time limit may spend reading files first
GRAPH_SHARE = 0.6        # of what a time limit leaves after indexing, for reading the graph's facts
# How long a query waits for its own vector. Awake, the worker answers in 6 to 20 ms. Asleep it
# takes half a second to wake: that query is answered by words and names, and the next one finds it awake.
QUERY_EMBED_SECONDS = 0.3
# A session's share of the machine-wide budget (ADR-005): four sessions and the worker's
# 490 MB worst case inside 1,024 MB. A session usually holds 40 to 60 MB.
SESSION_MEMORY_MB = 130
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
        self._graphs: dict[Path, tuple[tuple, object]] = {}  # per worktree: the code graph of the view last asked about
        self._last: Path | None = None          # the worktree the running operation uses
        self.memory_releases = 0
        self._catalogues: dict[Path, apsw.Connection] = {}   # one per repository, opened when first needed
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
        for conn in self._catalogues.values():
            try:
                conn.close()
            except apsw.Error:
                pass
        self._catalogues.clear()

    def relieve(self) -> dict | None:
        """Read this process's private memory; over the session's share, let go of what can
        be rebuilt: connections to worktrees other than the one in use, SQLite's page cache,
        the remembered trees and path words, the mapped vector file. Call between
        operations, on the thread that runs them. Returns what happened, or None when
        under the limit or when memory cannot be read here."""
        limit = float(os.environ.get("ALEPH_SESSION_MEMORY_MB", SESSION_MEMORY_MB))
        try:
            before = memstat.private_bytes()
        except memstat.MemstatError:
            return None
        if before <= limit * 1e6:
            return None
        for root in [root for root in self._indexes if root != self._last]:
            self._forget(root)
        for index in self._indexes.values():
            index.release_memory()
        for store in self._stores.values():
            store.release_memory()
        self._graphs.clear()
        views.forget_trees()
        forget_parsers()
        self._shown.clear()
        gc.collect()
        self.memory_releases += 1
        return {"before_mb": round(before / 1e6, 1), "after_mb": round(memstat.private_bytes() / 1e6, 1),
                "limit_mb": limit}

    def _memory(self) -> dict:
        try:
            used = round(memstat.private_bytes() / 1e6, 1)
        except memstat.MemstatError:
            used = None
        return {"private_mb": used, "limit_mb": float(os.environ.get("ALEPH_SESSION_MEMORY_MB", SESSION_MEMORY_MB)),
                "releases": self.memory_releases}

    def _forget(self, root: Path) -> None:
        index = self._indexes.pop(root, None)
        self._stores.pop(root, None)
        self._files.pop(root, None)
        self._graphs.pop(root, None)
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

    @_rebuilding
    def references(self, name: str, direction: str = "both", k: int = 50, cwd: Path | str | None = None,
                   timeout_ms: int | None = None, prepare_seconds: float | None = None) -> callgraph.Answer:
        """Who calls the definition(s) named `name`, and what they call ('callers', 'callees' or 'both'),
        at most `k` of each, the certain answers first. Each answer says how it was found and how many
        definitions the call may reach. The first question about a view reads every file's calls and
        imports, for at most `prepare_seconds` (or a share of the time limit); `graph_pending` says how
        many files are still unread, and a later question carries on."""
        if direction not in ("both", "callers", "callees"):
            raise ValueError("direction is 'callers', 'callees' or 'both'")
        started, until = _limits(timeout_ms)
        left = [0]

        def prepare(index: Index, view: View):
            if prepare_seconds is not None:
                budget = prepare_seconds
            else:
                budget = GRAPH_SECONDS if until is None else max(0.0, (until - time.monotonic()) * GRAPH_SHARE)
            left[0] = callgraph.prepare(index, view, budget, self._stop.is_set)
            if self._stop.is_set():
                raise Cancelled
            return (lambda: self._references(index, view, name, direction),)

        try:
            (found,), pending, late = self._consistent(cwd, started, until, prepare, ())
        except gitio.GitTimeout:
            return callgraph.Answer(name, [], [], [], late=True, elapsed_ms=_elapsed(started), direction=direction)
        if not found:       # out of time before the index could be read
            return callgraph.Answer(name, [], [], [], pending=pending, graph_pending=left[0], late=True,
                                    elapsed_ms=_elapsed(started), direction=direction)
        definitions, callers, callees, unanswered, truncated = found
        return callgraph.Answer(name, definitions, callers[:k], callees[:k], unanswered, pending, left[0],
                                late or left[0] > 0 and until is not None, _elapsed(started),
                                truncated or len(callers) > k or len(callees) > k, direction)

    def _references(self, index: Index, view: View, name: str, direction: str) -> tuple:
        definitions = symbols.lookup(index, view, name, limit=callgraph.DEFINITION_LIMIT + 1)
        if not definitions:
            return [], [], [], 0, False
        more = len(definitions) > callgraph.DEFINITION_LIMIT     # answered for the first few: said, not hidden
        definitions = definitions[:callgraph.DEFINITION_LIMIT]
        ready = callgraph.coverage(index)[0]
        token = (view.fingerprint, ready)
        cached = self._graphs.get(view.root)
        if cached is None or cached[0] != token:
            cached = (token, callgraph.load(index, view))
            self._graphs[view.root] = cached
        graph = cached[1]
        compiled = scipstore.Compiled(index.conn, view.files)     # what an imported compiler index says, where it may be believed
        compiled = compiled if compiled.any else None
        callers, truncated = callgraph.callers(index, graph, definitions, compiled) if direction != "callees" else ([], False)
        callees, unanswered = callgraph.callees(index, view, graph, definitions, compiled) if direction != "callers" else ([], 0)
        return definitions, callers, callees, unanswered, truncated or more

    @_rebuilding
    def scip_import(self, file: Path | str, tree: str | None = None, worktree: bool = False,
                    cwd: Path | str | None = None) -> dict:
        """Keep a SCIP index made elsewhere. Each document is stamped with the blob of its path in `tree`
        (default HEAD), or in the worktree as it is now with `worktree`: say what the index was made
        from, because a document is believed only where the view still holds that blob (ADR-004).
        Raises scipstore.NotAnIndex for a file that cannot be read as one."""
        view = self._view(cwd)
        index = self._index(view)
        kept, skipped, tool = scipstore.read(Path(file))
        if worktree:
            stamps, stamped = view.files, "worktree"
        else:
            stamped = tree or "HEAD"
            stamps = gitio.read_tree(view.root, stamped)[0]
        return scipstore.store(index.conn, kept, skipped, tool, stamps, stamped)

    @_rebuilding
    def scip_run(self, language: str | None = None, limit_mb: float | None = None, time_limit: float | None = None,
                 cwd: Path | str | None = None) -> dict:
        """Run the SCIP indexer for `language` (default: the worktree's main language) on the worktree,
        inside the memory limit, and keep what it wrote, stamped with the worktree as it was. Only on
        request: for a compiled language this runs the project's own build. Raises indexers.CannotRun
        when the language is import-only, the indexer is absent, it was stopped, or the worktree changed
        while it ran (an index of files that have moved on is not stamped as if it were of these)."""
        view = self._view(cwd)
        index = self._index(view)
        if language is None:
            index.sync(view, budget_seconds=0.0)
            language = next(iter(index.languages()), None)
            if language is None:
                raise indexers.CannotRun("no source code is indexed for this worktree yet: say which language with --lang")
        out = state_dir(view.common_dir) / f"scip-run-{os.getpid()}.scip"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            report = indexers.run(view.root, language, out, limit_mb, time_limit)
            now = self._view(cwd)
            moved = [path for path, oid in view.files.items() if now.files.get(path) != oid]
            if moved:
                raise indexers.CannotRun(f"{len(moved)} files changed while the indexer ran (first: {moved[0]}): "
                                         "nothing was kept. Run it again")
            kept, skipped, tool = scipstore.read(out)
            return {**report, "imported": scipstore.store(index.conn, kept, skipped, tool, view.files, "worktree")}
        finally:
            out.unlink(missing_ok=True)

    @_rebuilding
    def scip_imports(self, cwd: Path | str | None = None) -> list[dict]:
        return scipstore.imports(self._index(self._view(cwd)).conn)

    @_rebuilding
    def scip_forget(self, import_id: int | None = None, cwd: Path | str | None = None) -> int:
        return scipstore.forget(self._index(self._view(cwd)).conn, import_id)

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
            "graph": {**dict(zip(("files_read", "files_unread"), callgraph.coverage(index))),
                      "compiler_index_documents": index.conn.execute("SELECT count(*) FROM scip_documents").fetchall()[0][0]},
            "memory": {**self._memory(), **self._memory_counts(view)},
        }

    # ---- long-term memory -----------------------------------------------------------------------

    def _memories(self, view: View, create: bool = True) -> memory.Memory | None:
        """The repository's memories: a database of their own beside the index, which a
        rebuild of the index never touches. With `create` false, None when none was ever kept."""
        state = state_dir(view.common_dir)
        conn = self._catalogues.get(state)
        if conn is None:
            conn = memory.open_catalogue(state, create=create)
            if conn is None:
                return None
            self._catalogues[state] = conn
        return memory.Memory(conn, memory.open_store(state))

    def _memory_counts(self, view: View) -> dict:
        memories = self._memories(view, create=False)
        counts = memories.counts() if memories is not None else {"memories": 0, "with_vector": 0}
        return {**counts, "store": "aeon" if memory.open_store(state_dir(view.common_dir)) is not None else "none"}

    def _vector(self, text: str, kind: str, seconds: float):
        """The vector of a text from the embedding worker, or None when the worker is not
        installed, not running or not answering in time. Never starts it and never raises."""
        try:
            from .embed import setup, worker
            from .embed.client import EmbedClient, Unavailable

            sock = worker.socket_path()
            if not setup.installed() or not sock.exists():
                return None
            client = EmbedClient(sock, capacity=worker.capacity_tokens(), timeout=seconds)
            try:
                return client.embed([text], kind=kind)[0]
            except Unavailable:
                return None
        except Exception:  # noqa: BLE001 - memory works without vectors
            return None

    def remember(self, kind: str, text: str, cites: list[str] | None = None, session: str = "",
                 cwd: Path | str | None = None) -> dict:
        """Keep a memory of this repository. `cites` are `path:start-end` locations; those
        that are files of the worktree now are stored with the content they point at.
        Waits no longer for a vector than a query does: the caller is the server's one
        call thread, and a search must not queue behind a memory."""
        view = self._view(cwd)
        found = []
        for written in cites or ():
            match = _LOCATION.match(written.strip())
            if match and match.group(1) in view.files:
                start = int(match.group(2))
                end = int(match.group(3) or start)
                if end >= start:
                    found.append(memory.Cite(match.group(1), view.files[match.group(1)], start, end))
        kept = self._memories(view).remember(kind, text, session=session or "unnamed",
                                             vector=self._vector(text[:4000], "document", QUERY_EMBED_SECONDS), cites=found)
        return {"memory_id": kept, "cites": len(found)}

    def recall(self, query: str, k: int = 5, cwd: Path | str | None = None) -> list[memory.Recalled]:
        """Memories of this repository that fit the query, each with whether what it cites has changed."""
        view = self._view(cwd)
        memories = self._memories(view, create=False)
        if memories is None:
            return []
        return memories.recall(query, vector=self._vector(query, "query", QUERY_EMBED_SECONDS), k=k, view=view)

    def forget(self, memory_id: str | None = None, cwd: Path | str | None = None) -> int:
        """Forget one memory, or all of them with None. Returns how many were forgotten."""
        memories = self._memories(self._view(cwd), create=False)
        if memories is None:
            return 0
        return memories.forget_all() if memory_id is None else int(memories.forget(memory_id))

    def memories(self, limit: int = 50, cwd: Path | str | None = None) -> list[dict]:
        memories = self._memories(self._view(cwd), create=False)
        return memories.list(limit) if memories is not None else []

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
        if index is not None:
            path, opened = self._files[view.root]
            if store_db.inode(path) != opened:
                # Another session set a damaged index aside and built a new one. This session
                # may never have read the damage (its pages were in memory): it would go on
                # writing into the file that was set aside, where nobody else reads.
                for root in [root for root, (file, _) in self._files.items() if file == path]:
                    self._forget(root)
                index = None
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

_LOCATION = re.compile(r"(.+?):(\d+)(?:-(\d+))?\Z")


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
