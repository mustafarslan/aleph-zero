"""How much memory does each SCIP indexer need at its peak? ADR-004's open question.

Usage:
  uv run python bench/eval/scip_memory.py bench/data/graph [--corpus python-httpx] [--out FILE]

The protocol is fixed in the M9 plan, before any figure existed:

- each installed indexer runs once on its corpus (oxn's six, at the pinned commits), on a
  fresh copy of the tree, after the project's own offline install of its dependencies (which
  is measured with it); if that install cannot finish offline the indexer is not run and the
  language is "not measured here";
- the metric is the highest value, over samples every 100 ms, of the summed resident memory
  of the indexer and every process it started. Shared pages count once per process, so the
  figure is an upper bound: the safe direction for a limit;
- the limit registered beforehand is 2,048 MB. A language whose indexer finishes offline
  under it may be run on demand; any other is import-only.

A failed run is reported as failed, with its last lines. Nothing is retried with other settings.
The corpora are not written to: every run works in a temporary clone.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import temps  # noqa: E402

LIMIT_MB = 2048
SAMPLE_SECONDS = 0.1
TIME_LIMIT_SECONDS = 1800      # per corpus, the install of dependencies included
HEAT_LIMIT = 100.0
OFFLINE = {"GOFLAGS": "-mod=readonly", "GOPROXY": "off", "CARGO_NET_OFFLINE": "true", "npm_config_offline": "true"}
_NPM = ["--offline", "--ignore-scripts", "--no-audit", "--no-fund"]
# corpus -> (language, the offline install of its dependencies or None, the indexer's program, its arguments;
# {out} is where the index goes). In the order they are run: the quick ones first.
RUNS = {
    "python-httpx": ("python", None, "scip-python", ["index", ".", "--project-name", "httpx", "--output", "{out}"]),
    "go-kit": ("go", ["go", "mod", "download"], "scip-go", ["index", "--output", "{out}"]),
    "typescript-nest": ("typescript", ["npm", "ci", *_NPM], "scip-typescript", ["index", "--output", "{out}"]),
    "javascript-eslint": ("javascript", ["npm", "install", "--no-package-lock", *_NPM], "scip-typescript",
                          ["index", "--infer-tsconfig", "--output", "{out}"]),
    "rust-ripgrep": ("rust", ["cargo", "fetch", "--offline", "--locked"], "rust-analyzer", ["scip", ".", "--output", "{out}"]),
    "java-spring-petclinic": ("java", None, "scip-java", ["index", "--build-tool", "maven", "--output", "{out}", "--",
                                                          "--offline", "--batch-mode", "clean", "verify", "-DskipTests"]),
}


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


def version(program: str) -> str:
    for flag in ("--version", "-V", "version"):
        try:
            said = subprocess.run([program, flag], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        text = (said.stdout or said.stderr).strip().splitlines()
        if said.returncode == 0 and text:
            return text[0][:80]
    return "unknown"


def measure(command: list[str], cwd: Path, limit_mb: float | None = None, time_limit: float = TIME_LIMIT_SECONDS) -> dict:
    """Run `command`, sampling its family's memory. With `limit_mb`, the family is stopped when over it."""
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
            if now >= next_heat:
                hottest, next_heat = max(hottest, temps.hottest_chip(temps.reading())), now + 5.0
            if limit_mb is not None and peak_kb / 1024 > limit_mb:
                stopped = f"over {limit_mb:.0f} MB"
            elif now - started > time_limit:
                stopped = f"over {time_limit:.0f} s"
            elif hottest >= HEAT_LIMIT:
                stopped = f"chip at {hottest:.0f} C"
            if stopped:
                stop(process.pid, members)
                break
            time.sleep(SAMPLE_SECONDS)
        status = process.wait()
        output.seek(0)
        tail = output.read().decode("utf-8", "replace").strip().splitlines()[-6:]
    return {"peak_mb": round(peak_kb / 1024, 1), "seconds": round(time.monotonic() - started, 1), "exit": status,
            "processes": most, "samples": samples, "hottest_c": round(hottest, 1), "stopped": stopped, "last_lines": tail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path, help="holds <corpus>/ for each corpus")
    parser.add_argument("--corpus", action="append", choices=list(RUNS))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = {}

    def save() -> None:
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2) + "\n")

    for name in [each for each in RUNS if not args.corpus or each in args.corpus]:
        language, install, program, arguments = RUNS[name]
        source = args.workdir / name
        if shutil.which(program) is None or not source.is_dir():
            report[name] = {"language": language, "indexer": program, "ran": False,
                            "why": "the indexer is not installed" if shutil.which(program) is None else "the corpus is not prepared"}
            print(f"{name:24} not run: {report[name]['why']}", flush=True)
            save()
            continue
        commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="aleph-scip-") as scratch:
            copy, out = Path(scratch) / name, Path(scratch) / "index.scip"
            subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(source), str(copy)], check=True)
            command = [program, *[part.format(out=out) for part in arguments]]
            row = {"language": language, "indexer": program, "indexer_version": version(program), "corpus_commit": commit,
                   "command": " ".join(command), "install": " ".join(install) if install else None, "limit_mb": LIMIT_MB}
            prepared = measure(install, copy) if install else None
            if prepared is not None and (prepared["exit"] != 0 or prepared["stopped"]):
                report[name] = {**row, "ran": False, "install_run": prepared, "finished_offline": False, "under_limit": False,
                                "why": "its dependencies could not be installed offline: not measured here"}
                print(f"{name:24} {program:16} not measured: `{row['install']}` did not finish offline "
                      f"(exit {prepared['exit']}, {prepared['seconds']} s)", flush=True)
                for line in prepared["last_lines"]:
                    print("    ", line[:200])
                save()
                continue
            spent = prepared["seconds"] if prepared else 0.0
            found = measure(command, copy, time_limit=TIME_LIMIT_SECONDS - spent)
            size = out.stat().st_size if out.is_file() else 0
        finished = found["exit"] == 0 and size > 0 and not found["stopped"]
        peak = max(found["peak_mb"], prepared["peak_mb"] if prepared else 0.0)
        report[name] = {**row, "ran": True, **found, "install_run": prepared, "peak_mb_with_install": peak,
                        "seconds_with_install": round(found["seconds"] + spent, 1), "index_mb": round(size / 1e6, 1),
                        "finished_offline": finished, "under_limit": finished and peak <= LIMIT_MB}
        row = report[name]
        print(f"{name:24} {program:16} peak {peak:8.1f} MB (indexer {row['peak_mb']}, install {prepared['peak_mb'] if prepared else 0})  "
              f"{row['seconds_with_install']:6.1f} s  exit {row['exit']}  {row['processes']} processes  index {row['index_mb']} MB  "
              f"hottest {row['hottest_c']} C  {'on demand' if row['under_limit'] else 'import-only'}"
              f"{'  (' + row['stopped'] + ')' if row['stopped'] else ''}", flush=True)
        if not finished:
            for line in row["last_lines"]:
                print("    ", line[:200])
        save()
    save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
