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

from aleph_zero import indexers  # noqa: E402

LIMIT_MB = 2048
TIME_LIMIT_SECONDS = 1800      # per corpus, the install of dependencies included
HEAT_LIMIT = 100.0
_NPM = ["--ignore-scripts", "--no-audit", "--no-fund"]
# corpus -> (language, the install of its dependencies or None). The indexer's command is the product's own
# (`aleph_zero.indexers.INDEXERS`). In the order they are run: the quick ones first.
RUNS = {
    "python-httpx": ("python", None),
    "go-kit": ("go", ["go", "mod", "download"]),
    "typescript-nest": ("typescript", ["npm", "ci", *_NPM]),
    "javascript-eslint": ("javascript", ["npm", "install", "--no-package-lock", *_NPM]),
    "rust-ripgrep": ("rust", ["cargo", "fetch", "--locked"]),
    "java-spring-petclinic": ("java", None),
}
_OFFLINE_FLAG = {"npm": "--offline", "cargo": "--offline"}     # go is told by its environment


def install(command: list[str], cwd: Path, network: bool) -> dict:
    """Install a corpus's dependencies. Offline, it is watched like the indexer (it is what a run would
    cost). With the network (the owner's permission of 2026-10-12, for this and nothing else) it is only
    timed: its memory is the package manager's, and `aleph scip run` never installs."""
    if not network:
        flag = _OFFLINE_FLAG.get(command[0])
        return measure([*command, flag] if flag else command, cwd)
    started = time.monotonic()
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=TIME_LIMIT_SECONDS)
    return {"peak_mb": 0.0, "seconds": round(time.monotonic() - started, 1), "exit": done.returncode, "stopped": "",
            "with_network": True, "last_lines": (done.stdout + done.stderr).strip().splitlines()[-6:]}


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
    """Run `command` under the product's own watch (`aleph_zero.indexers.watch`: the code that guards
    `aleph scip run`), with the chip's temperature read beside it."""
    return indexers.watch(command, cwd, limit_mb, time_limit, heat=lambda: temps.hottest_chip(temps.reading()), heat_limit=HEAT_LIMIT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path, help="holds <corpus>/ for each corpus")
    parser.add_argument("--corpus", action="append", choices=list(RUNS))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--install-with-network", action="store_true",
                        help="install dependencies with the network (not measured), then run the indexer offline")
    args = parser.parse_args(argv)
    report = {}

    def save() -> None:
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2) + "\n")

    for name in [each for each in RUNS if not args.corpus or each in args.corpus]:
        language, installing = RUNS[name]
        program, arguments = indexers.INDEXERS[language].program, indexers.INDEXERS[language].arguments
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
            command = [program, *[part.format(out=out, name=name.split("-", 1)[1]) for part in arguments]]
            row = {"language": language, "indexer": program, "indexer_version": version(program), "corpus_commit": commit,
                   "command": " ".join(command), "install": " ".join(installing) if installing else None, "limit_mb": LIMIT_MB}
            prepared = install(installing, copy, args.install_with_network) if installing else None
            if prepared is not None and (prepared["exit"] != 0 or prepared["stopped"]):
                report[name] = {**row, "ran": False, "install_run": prepared, "finished_offline": False, "under_limit": False,
                                "why": "its dependencies could not be installed: not measured here"}
                print(f"{name:24} {program:16} not measured: `{row['install']}` did not finish "
                      f"{'with the network' if args.install_with_network else 'offline'} "
                      f"(exit {prepared['exit']}, {prepared['seconds']} s)", flush=True)
                for line in prepared["last_lines"]:
                    print("    ", line[:200])
                save()
                continue
            spent = prepared["seconds"] if prepared else 0.0
            found = measure(command, copy, time_limit=TIME_LIMIT_SECONDS - spent)
            size = out.stat().st_size if out.is_file() else 0
        finished = found["exit"] == 0 and size > 0 and not found["stopped"]
        peak = max(found["peak_mb"], prepared["peak_mb"] if prepared else 0.0)     # a networked install counts 0: see `install`
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
