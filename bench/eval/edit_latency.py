"""How long after one file is edited does an answer reflect it?

Usage:
  uv run python bench/eval/edit_latency.py CLONE [CLONE ...] [--times 15] [--out FILE]

oxn's figure for the same question is 140 ms median, 150 ms p95: a fresh process run by a hook
after an edit, on oxn's own repository (its ADR-0004); 62 ms warm, in process. ADR-007's bar for
Aleph is a one-file change reflected in under 150 ms from a cold process.

Each CLONE is a scratch clone that is already indexed (it is indexed here first if not). One
round: a function with a name that exists nowhere else is appended to a tracked Python file, and
the clock runs until a search for that name returns the file:
- cold: a new `aleph search` process, from its start to its exit (interpreter start, opening the
  index, reading the worktree's state from git, indexing the one file, the query);
- warm: the same question to an engine that is already open, as a session's server is.
The file is restored after each round. Medians and the 95th percentile over `--times` rounds.
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

from aleph_zero.engine import Engine


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def measure(root: Path, times: int) -> dict:
    files = subprocess.run(["git", "-C", str(root), "ls-files", "*.py"], capture_output=True, text=True, check=True).stdout.split()
    target = next(path for path in sorted(files, key=lambda path: (root / path).stat().st_size) if (root / path).stat().st_size > 2000)
    original = (root / target).read_bytes()
    program = [str(Path(sys.executable).parent / "aleph"), "--cwd", str(root), "search"]
    engine = Engine(root)
    while engine.search("warm up").pending:
        pass
    cold, warm = [], []
    try:
        for round_ in range(times * 2):
            name = f"zq_edit_probe_{round_}_{int(time.time())}"
            (root / target).write_bytes(original + f"\n\ndef {name}():\n    return {round_}\n".encode())
            started = time.perf_counter()
            if round_ % 2 == 0:
                shown = subprocess.run([*program, name], capture_output=True, text=True).stdout
                cold.append((time.perf_counter() - started) * 1000)
            else:
                answer = engine.search(name)
                warm.append((time.perf_counter() - started) * 1000)
                shown = " ".join(hit.path for hit in answer.hits)
            if target not in shown:
                raise SystemExit(f"the edit to {target} was not in the answer: {shown[:200]}")
    finally:
        (root / target).write_bytes(original)
        engine.search("settle")
        engine.close()
    return {"files": len(subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True).stdout.split("\n")) - 1,
            "edited": target, "rounds": times,
            "cold_ms": {"median": round(statistics.median(cold), 1), "p95": round(percentile(cold, 0.95), 1), "min": round(min(cold), 1)},
            "warm_ms": {"median": round(statistics.median(warm), 1), "p95": round(percentile(warm, 0.95), 1), "min": round(min(warm), 1)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("clones", type=Path, nargs="+")
    parser.add_argument("--times", type=int, default=15)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    report = {"oxn": {"cold_ms": {"median": 140, "p95": 150}, "warm_ms": 62}, "bar_cold_ms": 150,
              "clones": {clone.name: measure(clone.resolve(), args.times) for clone in args.clones}}
    print(json.dumps(report, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
