"""Part 17's speed reading: search p50 and p95, warm, with the module step off and on, on the seven repositories of
part 2 and the three held-out ones; and the first making of the summaries (cold, in a fresh process cache).

    area_speed.py --out FILE
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from aleph_zero import areas
from aleph_zero import search as search_module
from aleph_zero.engine import Engine

SCRATCH = Path("/private/tmp/claude-501/-Volumes-AI-SSD-Projects-aleph-zero/79c2c2f2-fc9d-4523-b030-d0e40ec1a033/scratchpad/facts/src")
HELDOUT = Path(__file__).resolve().parents[2] / "bench" / "data" / "heldout"
QUESTIONS = ["how are notifications delivered to users", "where is the password checked when a user logs in",
             "how are uploaded files stored", "what happens when a scheduled job fails", "how is the database connection set up"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    roots = sorted(SCRATCH.iterdir()) + [HELDOUT / name for name in ("gin-vue-admin", "ghostfolio", "healthchecks")]
    out = {}
    for root in roots:
        engine = Engine(root, dense=False)
        engine.search("warm", k=10)
        areas._views.clear()
        areas._bags.clear()
        started = time.monotonic()
        engine.prepare_areas()
        cold = time.monotonic() - started
        reading = {"first_summary_s": round(cold, 2)}
        for arm in ("off", "M2floor"):
            search_module.AREAS = arm
            times = []
            for _ in range(4):
                for question in QUESTIONS:
                    started = time.monotonic()
                    engine.search(question, k=10)
                    times.append((time.monotonic() - started) * 1000)
            times.sort()
            reading[arm] = {"p50_ms": round(statistics.median(times)), "p95_ms": round(times[int(len(times) * 0.95) - 1])}
        search_module.AREAS = "off"
        out[root.name] = reading
        print(root.name, reading, file=sys.stderr, flush=True)
        engine.close()
    args.out.write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
