"""Running a SCIP indexer on request, inside a memory limit (ADR-004).

Aleph never installs an indexer and never runs one for a query. `aleph scip run` runs the
one for a language whose peak memory was measured under the limit on a mid-size project
(M9: scip-python 459 MB, scip-java with Maven 881 MB, scip-go 948 MB, rust-analyzer
1,499 MB), watches the memory of everything it starts, and stops it all above the limit.
JavaScript is import-only (scip-typescript took 2,295 MB on eslint) and so is TypeScript
(not measured: the corpus's dependencies would not install): their index is made
elsewhere and imported.

An indexer for a compiled language runs the project's own build. That is the project's
code running on this machine: only ever on the owner's explicit request.
"""
from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LIMIT_MB = 2048.0               # ADR-004: twice ADR-005's standing budget, for a run that is requested, alone and short
TIME_LIMIT_SECONDS = 1800.0
SAMPLE_SECONDS = 0.1
# No network: each build tool is told to stay offline where it has a switch.
OFFLINE = {"GOFLAGS": "-mod=readonly", "GOPROXY": "off", "CARGO_NET_OFFLINE": "true", "npm_config_offline": "true"}


class CannotRun(RuntimeError):
    """The indexer was not run, or its run produced nothing to keep. The message says which and why."""


@dataclass(frozen=True)
class Indexer:
    language: str
    program: str
    arguments: tuple[str, ...]      # {out}: where the index goes; {name}: the project's name
    on_demand: bool                 # measured under the limit: may be run by `aleph scip run`
    how_to_get: str
    needs: str = ""                 # a file the project must have for the measured way of running it
    note: str = ""
    in_a_copy: bool = False         # the indexer runs the project's build: on a copy of the files, never in the worktree
    why_not: str = ""               # for an import-only language: what the measurement said


INDEXERS: dict[str, Indexer] = {
    "python": Indexer("python", "scip-python", ("index", ".", "--project-name", "{name}", "--output", "{out}"), True,
                      "npm install -g @sourcegraph/scip-python"),
    # The build compiles the sources and their tests and goes no further (no packaging, no integration tests),
    # in a copy of the files: `clean` there removes nothing of the owner's.
    "java": Indexer("java", "scip-java", ("index", "--build-tool", "maven", "--output", "{out}", "--",
                                          "--offline", "--batch-mode", "clean", "test-compile"), True,
                    "coursier install scip-java (sourcegraph.github.io/scip-java)", needs="pom.xml",
                    note="Maven projects: it compiles the project, offline, in a copy of its files", in_a_copy=True),
    "go": Indexer("go", "scip-go", ("index", "--output", "{out}"), True, "go install github.com/sourcegraph/scip-go/cmd/scip-go@latest",
                  needs="go.mod", note="the module's dependencies must already be in Go's module cache"),
    # rust-analyzer runs cargo, which runs the project's build scripts and writes a target directory: in a copy.
    "rust": Indexer("rust", "rust-analyzer", ("scip", ".", "--output", "{out}"), True, "rustup component add rust-analyzer",
                    needs="Cargo.toml", note="it runs cargo, offline, in a copy of the files", in_a_copy=True),
    "typescript": Indexer("typescript", "scip-typescript", ("index", "--output", "{out}"), False,
                          "npm install -g @sourcegraph/scip-typescript",
                          why_not="its memory has not been measured (the corpus's dependencies would not install)"),
    "javascript": Indexer("javascript", "scip-typescript", ("index", "--infer-tsconfig", "--output", "{out}"), False,
                          "npm install -g @sourcegraph/scip-typescript",
                          why_not="it was measured at 2,295 MB on eslint, over the limit"),
}


def limit_mb() -> float:
    """The most an indexer run may use, in megabytes. Raises CannotRun when ALEPH_SCIP_MEMORY_MB is set to
    something that is not a positive number: a limit that cannot be read must not become no limit."""
    raw = os.environ.get("ALEPH_SCIP_MEMORY_MB")
    if raw is None:
        return LIMIT_MB
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not (value > 0 and math.isfinite(value)):
        raise CannotRun(f"ALEPH_SCIP_MEMORY_MB is {raw!r}: it must be a number of megabytes above zero")
    return value


class _Blind(RuntimeError):
    """The processes could not be listed: nothing can be measured or stopped by name."""


PS = ["ps", "-A", "-o", "pid=,ppid=,pgid=,rss=,lstart="]


def processes() -> list[tuple[int, int, int, int, str]]:
    """(pid, parent, group, resident kilobytes, when it started) of every process."""
    try:
        listed = subprocess.run(PS, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _Blind(str(exc)) from exc
    rows = []
    for line in listed.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and all(part.lstrip("-").isdigit() for part in parts[:4]):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), parts[4]))
    if listed.returncode != 0 or not rows:
        raise _Blind(f"`{PS[0]}` ended with status {listed.returncode} and {len(rows)} rows")
    return rows


def family(root: int, rows: list[tuple[int, int, int, int, str]] | None = None) -> dict[int, tuple[int, str]]:
    """(resident kilobytes, start time) by pid of `root`, of everything in its process group and of every
    descendant by parentage (a child that moved to a group of its own is still the indexer's)."""
    rows = processes() if rows is None else rows
    mine = {pid for pid, _, pgid, _, _ in rows if pid == root or pgid == root}
    grew = True
    while grew:
        grew = False
        for pid, parent, _, _, _ in rows:
            if parent in mine and pid not in mine:
                mine.add(pid)
                grew = True
    return {pid: (rss, started) for pid, _, _, rss, started in rows if pid in mine}


def stop(root: int, seen: dict[int, str], reaped: bool = False) -> None:
    """Kill everything the run started. `seen` is every process ever seen in its family, with when each
    started: one that has since been left by its parent is nobody's child and in nobody's group, so it
    is found by having been seen, and killed only if a process of that number still started when it did
    (a number is reused). Before the indexer itself is reaped, its group is killed first: whatever
    joined it since the last look goes with it."""
    if not reaped:
        try:
            os.killpg(root, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        alive = {pid: started for pid, _, _, _, started in processes()}
    except _Blind:
        alive = dict(seen)      # cannot check: these were ours a moment ago
    for pid, started in seen.items():
        if alive.get(pid) == started and not (reaped and pid == root):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def watch(command: list[str], cwd: Path, limit: float | None = None, time_limit: float = TIME_LIMIT_SECONDS,
          heat: Callable[[], float] | None = None, heat_limit: float = 100.0) -> dict:
    """Run `command` in a session of its own, sampling the summed resident memory of everything it
    starts (shared pages count once per process: an upper bound). Above `limit` megabytes, past the
    time limit, with the chip at `heat_limit`, or when the processes cannot be listed, all of it is
    stopped and the result says why. Whatever it started is stopped when it ends, too.

    What this cannot see: a process started and abandoned by its parent between two looks (a tenth of
    a second apart) was never seen, and is neither counted nor stopped."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                   env={**os.environ, **OFFLINE})
        peak_kb, most, samples, hottest, stopped = 0, 0, 0, 0.0, ""
        seen: dict[int, str] = {}
        next_heat = 0.0
        while process.poll() is None:
            try:
                rows = processes()
                members = family(process.pid, rows)
                seen.update({pid: when for pid, (_, when) in members.items()})
                # Counted: the family now, and whatever was seen in it before and still runs though its parent left.
                current = {pid: (rss, when) for pid, _, _, rss, when in rows if seen.get(pid) == when}
                if not members and process.poll() is None:
                    raise _Blind("the indexer is running and is not in the list of processes")
            except _Blind as exc:
                stopped = f"could not watch its memory ({exc})"
                stop(process.pid, seen)
                break
            peak_kb, most, samples = max(peak_kb, sum(rss for rss, _ in current.values())), max(most, len(current)), samples + 1
            now = time.monotonic()
            if heat is not None and now >= next_heat:
                hottest, next_heat = max(hottest, heat()), now + 5.0
            if limit is not None and peak_kb / 1024 > limit:
                stopped = f"over {limit:.0f} MB"
            elif now - started > time_limit:
                stopped = f"over {time_limit:.0f} s"
            elif hottest >= heat_limit:
                stopped = f"chip at {hottest:.0f} C"
            if stopped:
                stop(process.pid, seen)
                break
            time.sleep(SAMPLE_SECONDS)
        status = process.wait()
        if not stopped:
            stop(process.pid, seen, reaped=True)    # whatever it left running behind it
        output.seek(0)
        tail = output.read().decode("utf-8", "replace").strip().splitlines()[-6:]
    return {"peak_mb": round(peak_kb / 1024, 1), "seconds": round(time.monotonic() - started, 1), "exit": status,
            "processes": most, "samples": samples, "hottest_c": round(hottest, 1), "stopped": stopped, "last_lines": tail}


def run(root: Path, language: str, out: Path, limit: float | None = None, time_limit: float | None = None,
        files: list[str] | None = None) -> dict:
    """Run the indexer for `language` on the worktree at `root`, writing to `out`. An indexer that runs
    the project's build works on a copy of `files` (the paths of the view), so that nothing it cleans
    or writes touches the worktree. Raises CannotRun when the language is import-only, the indexer is
    absent, or the run did not end with an index."""
    indexer = INDEXERS.get(language)
    if indexer is None:
        raise CannotRun(f"no SCIP indexer is known for {language}")
    if not indexer.on_demand:
        raise CannotRun(f"{language} is import-only: {indexer.program} is not run here because {indexer.why_not}. "
                        f"Run it yourself (or in CI) and keep the result with `aleph scip import`")
    if shutil.which(indexer.program) is None:
        raise CannotRun(f"{indexer.program} is not installed. Aleph does not install it; to get it: {indexer.how_to_get}")
    if indexer.needs and not (root / indexer.needs).is_file():
        raise CannotRun(f"{indexer.program} is run on request only the way it was measured ({indexer.note}); "
                        f"this worktree has no {indexer.needs}: import-only here")
    limit = limit_mb() if limit is None else limit
    command = [indexer.program, *[part.format(out=out, name=root.name) for part in indexer.arguments]]
    with tempfile.TemporaryDirectory(prefix="aleph-scip-") as scratch:
        where = root
        if indexer.in_a_copy:
            where = Path(scratch) / root.name
            for path in files if files is not None else []:
                source = root / path
                if source.is_file() and not source.is_symlink():
                    (where / path).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, where / path)
        found = watch(command, where, limit, TIME_LIMIT_SECONDS if time_limit is None else time_limit)
    if found["stopped"]:
        raise CannotRun(f"{indexer.program} was stopped: {found['stopped']} (peak {found['peak_mb']} MB, {found['seconds']} s). "
                        f"The limit is ALEPH_SCIP_MEMORY_MB; or make the index elsewhere and use `aleph scip import`")
    if found["exit"] != 0 or not out.is_file() or out.stat().st_size == 0:
        said = " | ".join(found["last_lines"][-3:])
        raise CannotRun(f"{indexer.program} ended with status {found['exit']} and no index: {said}")
    return {"language": language, "indexer": indexer.program, "command": " ".join(command), "limit_mb": limit, **found}


def present() -> list[tuple[Indexer, bool]]:
    """Every known indexer, and whether its program is on the PATH."""
    return [(indexer, shutil.which(indexer.program) is not None) for indexer in INDEXERS.values()]
