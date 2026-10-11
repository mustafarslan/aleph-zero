"""What lasts longer than one query: an index connection per worktree, time limits, and
what this session has already been shown.

The command line builds an engine for one call; the MCP server keeps one for the
session. Every operation takes the directory it is asked about, so one engine serves
any number of worktrees and repositories.
"""
from __future__ import annotations

import hashlib
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import apsw

from . import gitio, lang, render, sources, symbols
from . import outline as outlines
from .chunking import CHUNKER_VERSION
from .indexer import Index
from .search import search as run_search
from .store import SCHEMA_VERSION, index_file, open_db
from .view import View, build_view

SYNC_SECONDS = 5.0       # how long a query without a time limit may spend indexing first
SEARCH_SHARE = 0.25      # of a time limit, kept for the search itself
SHOWN_LIMIT = 5000       # texts remembered as shown before the memory starts over
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


class Engine:
    def __init__(self, default_cwd: Path | None = None) -> None:
        self.default_cwd = Path(default_cwd) if default_cwd is not None else Path.cwd()
        self._indexes: dict[Path, Index] = {}   # one connection per worktree: each holds one view
        self._shown: set[tuple] = set()
        self._stop = threading.Event()

    def cancel(self) -> None:
        """Stop the operation running on another thread; it raises Cancelled."""
        self._stop.set()

    def resume(self) -> None:
        """Forget a cancellation, before the next operation."""
        self._stop.clear()

    # ---- the four operations --------------------------------------------------------------

    def search(self, query: str, k: int = 8, kind: str = "any", path_glob: str | None = None,
               lang: str | None = None, cwd: Path | str | None = None,
               timeout_ms: int | None = None) -> render.Answer:
        families = _families(lang)
        started, until = _limits(timeout_ms)
        try:
            index, view, pending, late = self._ready(cwd, started, until)
        except gitio.GitTimeout:
            return render.Answer(query, [], 0, True, _elapsed(started))  # not even the worktree was read in time
        hits = []
        if not late or time.monotonic() < until:
            try:
                with deadline(index.conn, until, self._stop):
                    hits = run_search(index, view, query, k=k, kind=kind, path_glob=path_glob, families=families) or []
            except apsw.InterruptError:
                late = True
                _reset(index.conn)
        if self._stop.is_set():
            raise Cancelled
        return render.Answer(query, hits, pending, late, _elapsed(started))

    def symbol(self, name: str, k: int = 20, cwd: Path | str | None = None,
               timeout_ms: int | None = None) -> SymbolAnswer:
        started, until = _limits(timeout_ms)
        try:
            index, view, pending, late = self._ready(cwd, started, until)
        except gitio.GitTimeout:
            return SymbolAnswer(name, [], 0, True, _elapsed(started))
        found = []
        try:
            with deadline(index.conn, until, self._stop):
                found = symbols.lookup(index, view, name, limit=k)
        except apsw.InterruptError:
            late = True
            _reset(index.conn)
        if self._stop.is_set():
            raise Cancelled
        return SymbolAnswer(name, found, pending, late, _elapsed(started))

    def outline(self, path: str, cwd: Path | str | None = None,
                timeout_ms: int | None = None) -> outlines.Outline:
        """Raises gitio.GitTimeout when the worktree cannot be read within the limit."""
        with gitio.until(_limits(timeout_ms)[1]):
            return outlines.of(self._view(cwd), path)

    def status(self, cwd: Path | str | None = None, timeout_ms: int | None = None) -> dict:
        """Never indexes. Raises gitio.GitTimeout when the worktree cannot be read within the limit."""
        with gitio.until(_limits(timeout_ms)[1]):
            view = self._view(cwd)
        index = self._index(view)
        pending = index.sync(view, budget_seconds=0.0).pending
        counts = index.counts()
        try:
            linked, problem = sources.describe(view.common_dir), ""
        except sources.SourceError as exc:
            linked, problem = [], str(exc)
        return {
            "root": str(view.root), "files_in_view": len(view.files), "dirty": len(view.dirty), "pending": pending,
            "chunks_in_view": counts["chunks_in_view"], "symbols_in_view": counts["symbols_in_view"],
            "imports_in_view": counts["imports_in_view"], "index": str(index_file(view.common_dir)),
            "schema_version": SCHEMA_VERSION, "chunker_version": CHUNKER_VERSION,
            "sqlite": apsw.sqlite_lib_version(), "untracked_cache": gitio.untracked_cache_enabled(view.root),
            "sources": linked, **({"sources_error": problem} if problem else {}),
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

    def _index(self, view: View) -> Index:
        index = self._indexes.get(view.root)
        if index is None:
            index = self._indexes[view.root] = Index(open_db(index_file(view.common_dir)))
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
        return index, view, report.pending, until is not None and report.pending > 0


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
