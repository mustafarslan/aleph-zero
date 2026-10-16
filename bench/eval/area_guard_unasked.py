"""Part 17's order reading on the 158 never-asked Python questions (the second Python draw of DEFERRED 1d), asked as
`area_guard.py` asks part 1's: by first line, words and names, off against M2floor and M2, each arm's ten files kept.

    area_guard_unasked.py --out FILE
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402
from area_guard import score_row  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
WORKDIR = ROOT / "bench" / "data" / "benchc-python-unasked"
ASKED = ROOT / "bench" / "data" / "results" / "benchmark_c_400.json"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    prepared = {path.name.replace("__", "/", 1) for path in WORKDIR.iterdir() if (path / ".git").exists()}
    asked = bench.asked_in([ASKED])
    rows = [row for row in bench.instances(prepared, "locbench", None, None) if row["instance_id"] not in asked]
    with args.out.open("w") as out:
        for number, row in enumerate(rows, 1):
            entry = score_row(row, WORKDIR, None, False)
            if entry is None:
                print(f"{number:4}/{len(rows)} {row['instance_id']}: not prepared, left out", file=sys.stderr, flush=True)
                continue
            out.write(json.dumps(entry) + "\n")
            out.flush()
    print(len(rows), "questions", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
