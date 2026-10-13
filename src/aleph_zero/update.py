"""Bringing a repository up to date in one go (ADR-014): for a person, a git hook or a nightly schedule.

Nothing here is made for a project as a whole. Each part is kept per content and only what changed
is read; this does at once what the engine would do at the next question, and the two things that
nothing does by itself: fetching the sources linked by URL, and making vectors while no server runs.
The order matters, each part feeds the next: sources, index, facts, vectors.
"""
from __future__ import annotations

import fcntl
import hashlib
import shlex
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import apsw

from . import archfacts, callgraph, gitio, scipstore, sources, vectorfile
from .indexer import Index, collect_if_due
from .store import StoreError, index_file, open_db, state_dir
from .view import build_view

EMBED_MINUTES = 10.0     # of embedding in one update unless told otherwise; `aleph embed run` is the unbounded one
HOOK_MINUTES = 5         # in the lines printed for git's hooks: a pull is not held up for it, but it is not left running for long either
FACTS_SECONDS = 60.0     # of reading the graph's and the architecture's facts
ROUNDS = 3               # times one update looks again at a worktree that moved on while it worked
BUSY = "another `aleph update` is running for this worktree"
# What a part may run into without the parts after it being lost, and without a traceback in a hook's output.
# apsw's errors are not RuntimeErrors: an index another session is writing to says "busy" through them.
_FAILURES = (gitio.GitError, StoreError, sources.SourceError, apsw.Error, OSError, RuntimeError)


@contextmanager
def only_one(common_dir: Path, root: Path):
    """Held by the one update of a worktree. Yields False at once when another holds it: rapid checkouts start
    several, and the one at work looks again at the worktree before it ends. A worktree, not the repository:
    the index is made for several sessions at once, and another worktree's update is not held up by this one."""
    directory = state_dir(common_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]
    with open(directory / f"update-{name}.lock", "w") as handle:
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


def _empty() -> dict:
    """The report's shape, the same whatever happened: a script reads the keys it expects."""
    return {"busy": None, "sources": {}, "indexed": 0, "skipped": 0, "pending": 0,
            "graph": {"read": 0, "left": 0, "why": ""}, "architecture": {"read": 0, "left": 0, "why": ""},
            "compiler_indexes": {"out_of_date": 0, "how": ""}, "vectors": {"made": 0, "left": 0, "why": ""}, "failed": []}


def run(cwd: Path, fetch: bool = True, minutes: float = EMBED_MINUTES, dense: bool | None = None) -> dict:
    """Bring the worktree of `cwd` up to date and say what was done. A part that fails is named in `failed`
    and the parts after it are still tried. `dense` is the caller's choice for this process (ALEPH_DENSE), as
    for a search; None follows the repository's kept choice and its language's verdict."""
    from .engine import Engine   # the engine imports much; this module is also read for its constants

    view = build_view(cwd)
    report = _empty()
    with only_one(view.common_dir, view.root) as mine:
        if not mine:
            report["busy"] = BUSY
            return report

        def part(name: str, do) -> bool:
            try:
                do()
                return True
            except _FAILURES as problem:
                report["failed"].append(f"{name}: {problem}")
                return False

        def fetch_sources() -> None:
            if view.plain or not fetch:
                return
            if any(row["kind"] == "url" for row in sources.describe(view.common_dir)):
                # Only the sources that are fetched are reported; one read in place has nothing to say here.
                report["sources"] = {name: outcome for name, outcome in sources.refresh(cwd, unattended=True).items()
                                     if not outcome.startswith("local:")}

        part("sources", fetch_sources)
        deadline = time.monotonic() + max(minutes, 0.0) * 60.0
        seen = None
        for _ in range(ROUNDS):
            # A checkout that lands while this is at work would start an update that finds this one busy and
            # ends: so before it ends this one looks again, and does the same for what moved on.
            now = build_view(cwd)
            if seen is not None and now.fingerprint == seen.fingerprint:
                break
            seen = _round(cwd, now, report, part, Engine, dense, deadline)
        return report


def _round(cwd: Path, view, report: dict, part, engine_of, dense: bool | None, deadline: float):
    """One pass over the worktree as it is now: index, facts, compiler indexes, vectors. Returns the view it ended on."""
    seen = view
    index: Index | None = None

    def index_all() -> None:
        nonlocal seen, index
        index = Index(open_db(index_file(view.common_dir)))
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
        if report["indexed"] or report["skipped"]:
            # What a query does after it stored new content: old content may be due to go. Where a hook runs this
            # after every pull, a query never finds anything new to store, and nothing else would ever collect.
            try:
                collected = collect_if_due(index.conn)
                if collected and collected.get("chunks_removed"):
                    vectorfile.give_back(index.conn, index_file(view.common_dir).parent)
            except (apsw.BusyError, OSError):
                pass                                # another session has the turn, or the disk says no: next time

    def facts(name: str, table: str, module) -> None:
        # An index that is only searched pays nothing for these (the schema's rule): they are read for what
        # changed where they were ever asked for, and not begun where they were not.
        if not _in_use(index, table):
            report[name] = {"read": report[name]["read"], "left": 0, "why": "never asked for in this repository, so not begun"}
            return
        before = len(module.pending(index))
        left = module.prepare(index, seen, FACTS_SECONDS) if before else 0
        report[name] = {"read": report[name]["read"] + before - left, "left": left, "why": ""}

    def compiler_indexes() -> None:
        compiled = scipstore.Compiled(index.conn, seen.files)
        stale = sum(1 for path in seen.files if compiled.status(path)) if compiled.any else 0
        report["compiler_indexes"] = {"out_of_date": stale, "how": "`aleph scip run` makes them again" if stale else ""}

    def vectors() -> None:
        # Where search by meaning is on already: the verdict of the language, or the choice kept by `aleph setup`.
        # Never switched on here: a nightly hook would otherwise switch it on in every repository.
        engine = engine_of(cwd, dense=dense)
        try:
            status = engine.status(cwd)["vectors"]
            made = report["vectors"]["made"]
            if not status["enabled"]:
                report["vectors"] = {"made": made, "left": 0, "why": status["why"]}
                return
            if status["with_vector"] >= status["chunks"]:
                # Nothing to make: the worker is not started for it. After most pulls this is the case, and
                # starting it loads a model of 300 MB to be told so.
                report["vectors"] = {"made": made, "left": 0, "why": ""}
                return
            done = engine.embed(cwd, budget_seconds=max(deadline - time.monotonic(), 0.0), sync_seconds=0.0)
            report["vectors"] = {"made": made + done.embedded, "left": done.remaining, "why": done.reason}
        finally:
            engine.close()

    try:
        if part("index", index_all):                # without the index there is nothing the next three could read
            part("graph", lambda: facts("graph", "graph_facts", callgraph))
            part("architecture", lambda: facts("architecture", "arch_facts", archfacts))
            part("compiler indexes", compiler_indexes)
    finally:
        if index is not None:
            index.conn.close()
    part("vectors", vectors)
    return seen


def text(report: dict) -> list[str]:
    """What an update did, a line a part; one line when nothing changed."""
    if report.get("busy"):
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
    """What to put into git's hooks, printed and never installed. The engine by the place it is in, quoted (a
    hook's PATH is not a shell's, and a path may hold a space); detached, silent and reading nothing, so that
    `git pull` and `git checkout` return at once; without the network, which a hook has no business using on
    every change of branch; the embedding bounded; not in the middle of a rebase or a bisect, where git checks
    out commit after commit that nobody will ask about; and for post-checkout only on a change of branch, written
    so that the hook ends well when it is not one: git calls it for one restored file too, and a hook that fails
    fails the command."""
    from .firstrun import engine_path

    engine = shlex.quote(engine_path())
    command = f"( {engine} update --quiet --no-fetch --minutes {HOOK_MINUTES} </dev/null >/dev/null 2>&1 & )"
    not_now = ['d=$(git rev-parse --git-dir 2>/dev/null) || exit 0',
               'if [ -d "$d/rebase-merge" ] || [ -d "$d/rebase-apply" ] || [ -f "$d/BISECT_LOG" ]; then exit 0; fi']
    return "\n".join([
        "# Aleph does not install hooks or change git's configuration. To have a repository brought up to date",
        "# after every pull and every change of branch, put these lines into its hooks yourself and make the two",
        "# files executable. Where the hooks are: `git rev-parse --git-path hooks` (in a linked worktree `.git`",
        "# is a file, and the hooks are the main repository's).",
        "",
        "# .git/hooks/post-merge",
        "#!/bin/sh",
        *not_now,
        command,
        "",
        "# .git/hooks/post-checkout",
        "#!/bin/sh",
        '[ "$3" = "1" ] || exit 0',
        *not_now,
        command,
        "",
        "# Or nightly, from cron or launchd, with the network (linked sources are fetched) and more time:",
        f"#   {engine} --cwd /path/to/repository update --quiet --minutes 60",
    ])
