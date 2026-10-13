"""Bringing a repository up to date in one go (ADR-014): for a person, a git hook or a nightly schedule.

Nothing here is made for a project as a whole. Each part is kept per content and only what changed
is read; this does at once what the engine would do at the next question, and the two things that
nothing does by itself: fetching the sources linked by URL, and making vectors while no server runs.
The order matters, each part feeds the next: sources, index, facts, vectors.
"""
from __future__ import annotations

import fcntl
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import archfacts, callgraph, gitio, scipstore, sources
from .indexer import Index
from .store import StoreError, index_file, open_db, state_dir
from .view import build_view

EMBED_MINUTES = 10.0     # of embedding in one update unless told otherwise; `aleph embed run` is the unbounded one
HOOK_MINUTES = 5         # in the lines printed for git's hooks: a pull is not held up for it, but it is not left running for long either
FACTS_SECONDS = 60.0     # of reading the graph's and the architecture's facts
BUSY = "another `aleph update` is running for this repository"
_FAILURES = (gitio.GitError, StoreError, sources.SourceError, OSError, RuntimeError)


@contextmanager
def only_one(common_dir: Path):
    """Held by the one update of a repository. Yields False at once when another holds it: rapid checkouts
    start several, and the second has nothing to add to what the first is doing."""
    directory = state_dir(common_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "update.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _in_use(index: Index, table: str) -> bool:
    return bool(index.conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchall())


def run(cwd: Path, fetch: bool = True, minutes: float = EMBED_MINUTES) -> dict:
    """Bring the worktree of `cwd` up to date and say what was done. A part that fails is named in `failed`
    and the parts after it are still tried."""
    from .engine import Engine   # the engine imports much; this module is also read for its constants

    view = build_view(cwd)
    with only_one(view.common_dir) as mine:
        if not mine:
            return {"busy": BUSY}
        report: dict = {"sources": {}, "indexed": 0, "skipped": 0, "pending": 0, "failed": []}

        def part(name: str, do) -> None:
            try:
                do()
            except _FAILURES as problem:
                report["failed"].append(f"{name}: {problem}")

        def fetch_sources() -> None:
            if view.plain or not fetch:
                return
            found = sources.describe(view.common_dir)
            if any(row["kind"] == "url" for row in found):
                # Only the sources that are fetched are reported; one read in place has nothing to say here.
                report["sources"] = {name: outcome for name, outcome in sources.refresh(cwd).items() if not outcome.startswith("local:")}

        part("sources", fetch_sources)

        index, seen = Index(open_db(index_file(view.common_dir))), build_view(cwd)     # the view again: a fetch moved a source's tree

        def index_all() -> None:
            nonlocal seen
            while True:
                done = index.sync(seen, budget_seconds=30.0)
                report["indexed"] += done.indexed
                report["skipped"] += done.skipped
                report["pending"] = done.pending
                if done.pending == 0:
                    break
                fresh = build_view(cwd)                 # the worktree may have moved on while this indexed
                if done.indexed + done.skipped == 0 and fresh.fingerprint == seen.fingerprint:
                    break                               # nothing more can be done right now
                seen = fresh

        def facts(name: str, table: str, module) -> None:
            # An index that is only searched pays nothing for these (the schema's rule): they are read for what
            # changed where they were ever asked for, and not begun where they were not.
            if not _in_use(index, table):
                report[name] = {"read": 0, "left": 0, "why": "never asked for in this repository, so not begun"}
                return
            before = len(module.pending(index))
            left = module.prepare(index, seen, FACTS_SECONDS) if before else 0
            report[name] = {"read": before - left, "left": left, "why": ""}

        def compiler_indexes() -> None:
            compiled = scipstore.Compiled(index.conn, seen.files)
            stale = sum(1 for path in seen.files if compiled.any and compiled.status(path)) if compiled.any else 0
            report["compiler_indexes"] = {"out_of_date": stale, "how": "`aleph scip run` makes them again" if stale else ""}

        try:
            part("index", index_all)
            part("graph", lambda: facts("graph", "graph_facts", callgraph))
            part("architecture", lambda: facts("architecture", "arch_facts", archfacts))
            part("compiler indexes", compiler_indexes)
        finally:
            index.conn.close()

        def vectors() -> None:
            # Where search by meaning is on already: the verdict of the language, or the choice kept by `aleph setup`.
            # Never switched on here: a nightly hook would otherwise switch it on in every repository.
            engine = Engine(cwd)
            try:
                status = engine.status(cwd)["vectors"]
                if not status["enabled"]:
                    report["vectors"] = {"made": 0, "left": 0, "why": status["why"]}
                    return
                started = time.monotonic()
                done = engine.embed(cwd, budget_seconds=max(minutes, 0.0) * 60.0, sync_seconds=0.0)
                report["vectors"] = {"made": done.embedded, "left": done.remaining, "why": done.reason,
                                     "seconds": round(time.monotonic() - started, 1)}
            finally:
                engine.close()

        part("vectors", vectors)
        return report


def text(report: dict) -> list[str]:
    """What an update did, a line a part; one line when nothing changed."""
    if "busy" in report:
        return [report["busy"]]
    files = lambda count: f"{count} file" + ("" if count == 1 else "s")  # noqa: E731
    lines = [f"source {name}: {outcome}" for name, outcome in report.get("sources", {}).items() if outcome != "unchanged"]
    if report.get("indexed"):
        lines.append(f"indexed {files(report['indexed'])}" + (f"; {report['pending']} still to do" if report.get("pending") else ""))
    for name, what in (("graph", "the code graph's facts"), ("architecture", "the architecture's facts")):
        done = report.get(name) or {}
        if done.get("read") or done.get("left"):
            lines.append(f"read {what} of {files(done['read'])}" + (f"; {done['left']} still to read" if done["left"] else ""))
    made = report.get("vectors") or {}
    if made.get("made") or made.get("left"):
        lines.append(f"made {made['made']} vector" + ("" if made["made"] == 1 else "s")
                     + (f"; {made['left']} chunks still without one" if made["left"] else "") + (f" ({made['why']})" if made.get("why") else ""))
    stale = (report.get("compiler_indexes") or {}).get("out_of_date")
    if stale:
        lines.append(f"a compiler index is out of date for {files(stale)}: `aleph scip run` makes them again")
    lines += [f"FAILED {problem}" for problem in report.get("failed", [])]
    return lines or ["nothing changed: the index matches the worktree"]


def hook_lines() -> str:
    """What to put into git's hooks, printed and never installed. The engine by the place it is in (a hook's
    PATH is not a shell's); detached and silent, so that `git pull` and `git checkout` return at once; the
    embedding bounded; and for post-checkout only on a change of branch, since git calls it for one restored file too."""
    engine = Path(sys.executable).with_name("aleph")
    command = f'( {engine} update --quiet --minutes {HOOK_MINUTES} >/dev/null 2>&1 & )'
    return "\n".join([
        "# Aleph does not install hooks or change git's configuration. To have a repository brought up to date",
        "# after every pull and every change of branch, put these lines into its hooks yourself (and make the",
        "# files executable: chmod +x .git/hooks/post-merge .git/hooks/post-checkout).",
        "",
        "# .git/hooks/post-merge",
        "#!/bin/sh",
        command,
        "",
        "# .git/hooks/post-checkout",
        "#!/bin/sh",
        f'[ "$3" = "1" ] && {command}',
        "",
        "# Or nightly, from cron or launchd, with the network and more time:",
        f"#   {engine} --cwd /path/to/repository update --quiet --minutes 60",
    ])
