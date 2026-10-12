"""At how many vectors does scanning every one of them stop fitting a query?

Usage:
  uv run python bench/eval/scan_limit.py SCRATCH_DIR [--out FILE]

The protocol is fixed in the M10 plan before any figure: synthetic unit vectors of the stored
width in a float16 file, scanned as `vectors.VectorStore.search` scans them (rows taken from a
memory map in blocks, converted to float32, one matrix product, the best 300 kept), warm, the
median of 15, at 100,000, 300,000, 1,000,000 and 3,000,000 rows. The limit is where the scan
alone passes 50 ms, or the memory the scan holds privately (scores, the view's row and chunk
numbers, one block of scratch) passes 64 MB. Two layouts of the view's rows are timed: side by
side (a fresh index), and with every seventh row another view's (the vectors of several branches
in one file); the limit is read from the slower. The mapped file itself is shared by the system
across sessions and is not private memory (ADR-005).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

from aleph_zero import vectors

SIZES = (100_000, 300_000, 1_000_000, 3_000_000)
TIME_LIMIT_MS, MEMORY_LIMIT_MB, KEPT, ROUNDS = 50.0, 64.0, 300, 15
STORED = 3_500_000      # rows in the file: enough for the largest size when every seventh row is another view's


def scan(mapped: np.memmap, rows: np.ndarray, query: np.ndarray) -> np.ndarray:
    scores = vectors.scores_of(mapped, rows, query)      # the engine's own scan
    best = np.argpartition(-scores, KEPT - 1)[:KEPT]
    return best[np.argsort(-scores[best], kind="stable")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("scratch", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    path = args.scratch / "scan-limit.f16"
    random = np.random.default_rng(20261012)
    report = {"width": vectors.WIDTH, "block": vectors._BLOCK, "limits": {"ms": TIME_LIMIT_MS, "private_mb": MEMORY_LIMIT_MB}, "sizes": {}}
    try:
        with open(path, "wb") as file:
            for _ in range(0, STORED, 100_000):
                block = random.standard_normal((100_000, vectors.WIDTH), dtype=np.float32)
                block /= np.linalg.norm(block, axis=1, keepdims=True)
                file.write(block.astype(np.float16).tobytes())
        mapped = np.memmap(path, dtype=np.float16, mode="r", shape=(STORED, vectors.WIDTH))
        query = mapped[7].astype(np.float32)
        for size in SIZES:
            entry = report["sizes"][str(size)] = {"file_mb": round(size * vectors.ROW_BYTES / 1e6)}
            every = np.arange(STORED, dtype=np.int64)
            for layout, rows in (("side_by_side", every[:size]), ("with_gaps", np.delete(every, np.arange(3, STORED, 7))[:size])):
                assert len(rows) == size
                chunk_ids = rows.copy()
                scan(mapped, rows, query)   # warm: the file's pages are in the system's cache
                took = []
                for _ in range(ROUNDS):
                    started = time.perf_counter()
                    best = scan(mapped, rows, query)
                    took.append((time.perf_counter() - started) * 1000)
                assert int(chunk_ids[best[0]]) == 7
                private = (size * 4 + rows.nbytes + chunk_ids.nbytes + vectors._BLOCK * vectors.WIDTH * 4) / 1e6
                entry[layout] = {"median_ms": round(statistics.median(took), 1), "worst_ms": round(max(took), 1)}
                entry["private_mb"] = round(private, 1)
    finally:
        path.unlink(missing_ok=True)
    print(json.dumps(report, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
