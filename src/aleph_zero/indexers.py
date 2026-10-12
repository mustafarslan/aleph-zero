"""Running a SCIP indexer on request, inside a memory limit (ADR-004).

Aleph never installs an indexer and never runs one for a query. `aleph scip run` runs the
one for a language whose peak memory was measured under the limit on a mid-size project
(M9: scip-python 459 MB, scip-java with Maven 896 MB), watches the memory of everything
it starts, and stops it all above the limit. The other languages are import-only until
measured: their indexer is made to run elsewhere and the result imported.

An indexer for a compiled language runs the project's own build. That is the project's
code running on this machine: only ever on the owner's explicit request.
"""
from __future__ import annotations

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


INDEXERS: dict[str, Indexer] = {
    "python": Indexer("python", "scip-python", ("index", ".", "--project-name", "{name}", "--output", "{out}"), True,
                      "npm install -g @sourcegraph/scip-python"),
    "java": Indexer("java", "scip-java", ("index", "--build-tool", "maven", "--output", "{out}", "--",
                                          "--offline", "--batch-mode", "clean", "verify", "-DskipTests"), True,
                    "coursier install scip-java (sourcegraph.github.io/scip-java)", needs="pom.xml",
                    note="Maven projects: it runs the project's own build, offline"),
    "typescript": Indexer("typescript", "scip-typescript", ("index", "--output", "{out}"), False,
                          "npm install -g @sourcegraph/scip-typescript"),
    "javascript": Indexer("javascript", "scip-typescript", ("index", "--infer-tsconfig", "--output", "{out}"), False,
                          "npm install -g @sourcegraph/scip-typescript"),
    "go": Indexer("go", "scip-go", ("index", "--output", "{out}"), False, "go install github.com/sourcegraph/scip-go/cmd/scip-go@latest"),
    "rust": Indexer("rust", "rust-analyzer", ("scip", ".", "--output", "{out}"), False, "rustup component add rust-analyzer"),
}


def limit_mb() -> float:
    return float(os.environ.get("ALEPH_SCIP_MEMORY_MB", LIMIT_MB))


def family(root: int) -> dict[int, int]:
    """Resident kilobytes of `root`, of everything in its process group and of every descendant by
    parentage (a child that moved to a group of its own is still the indexer's)."""
    listed = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,pgid=,rss="], capture_output=True, text=True).stdout
    rows = [tuple(int(part) for part in line.split()) for line in listed.splitlines() if len(line.split()) == 4]
    mine = {pid for pid, _, pgid, _ in rows if pid == root or pgid == root}
    grew = True
    while grew:
        grew = False
        for pid, parent, _, _ in rows:
            if parent in mine and pid not in mine:
                mine.add(pid)
                grew = True
    return {pid: rss for pid, _, _, rss in rows if pid in mine}


def stop(root: int, members: dict[int, int]) -> None:
    """Kill the group first (whatever joined it since the last look goes with it), then every descendant
    that had moved to a group of its own, then look once more for stragglers."""
    for target in (lambda: os.killpg(root, signal.SIGKILL), *[lambda pid=pid: os.kill(pid, signal.SIGKILL) for pid in members]):
        try:
            target()
        except (ProcessLookupError, PermissionError):
            pass
    for pid in family(root):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def watch(command: list[str], cwd: Path, limit: float | None = None, time_limit: float = TIME_LIMIT_SECONDS,
          heat: Callable[[], float] | None = None, heat_limit: float = 100.0) -> dict:
    """Run `command` in a session of its own, sampling the summed resident memory of everything it
    starts (shared pages count once per process: an upper bound). Above `limit` megabytes, past the
    time limit, or with the chip at `heat_limit`, all of it is stopped and the result says why."""
    started = time.monotonic()
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                   env={**os.environ, **OFFLINE})
        peak_kb, most, samples, hottest, stopped = 0, 0, 0, 0.0, ""
        next_heat = 0.0
        while process.poll() is None:
            members = family(process.pid)
            peak_kb, most, samples = max(peak_kb, sum(members.values())), max(most, len(members)), samples + 1
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
                stop(process.pid, members)
                break
            time.sleep(SAMPLE_SECONDS)
        status = process.wait()
        if not stopped:
            stop(process.pid, {})       # whatever it left running behind it
        output.seek(0)
        tail = output.read().decode("utf-8", "replace").strip().splitlines()[-6:]
    return {"peak_mb": round(peak_kb / 1024, 1), "seconds": round(time.monotonic() - started, 1), "exit": status,
            "processes": most, "samples": samples, "hottest_c": round(hottest, 1), "stopped": stopped, "last_lines": tail}


def run(root: Path, language: str, out: Path, limit: float | None = None, time_limit: float | None = None) -> dict:
    """Run the indexer for `language` on the worktree at `root`, writing to `out`. Raises CannotRun when
    the language is import-only, the indexer is absent, or the run did not end with an index."""
    indexer = INDEXERS.get(language)
    if indexer is None:
        raise CannotRun(f"no SCIP indexer is known for {language}")
    if not indexer.on_demand:
        raise CannotRun(f"{language} is import-only: the memory of {indexer.program} has not been measured under the limit here. "
                        f"Run it yourself (or in CI) and keep the result with `aleph scip import`")
    if shutil.which(indexer.program) is None:
        raise CannotRun(f"{indexer.program} is not installed. Aleph does not install it; to get it: {indexer.how_to_get}")
    if indexer.needs and not (root / indexer.needs).is_file():
        raise CannotRun(f"{indexer.program} is run on request only the way it was measured ({indexer.note}); "
                        f"this worktree has no {indexer.needs}: import-only here")
    limit = limit_mb() if limit is None else limit
    command = [indexer.program, *[part.format(out=out, name=root.name) for part in indexer.arguments]]
    found = watch(command, root, limit, TIME_LIMIT_SECONDS if time_limit is None else time_limit)
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
