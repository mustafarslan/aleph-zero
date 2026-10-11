"""Does searching for a definition's name return that definition first?

Usage: uv run python bench/spikes/definition_check.py REPO [REPO ...] [--sample N] [--out FILE]

For each repository: a cold index (time, private memory, counts), then for a random
sample of functions, methods and types whose name is defined exactly once in the view,
search for the bare name and record where the definition lands.
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
import statistics
import sys
import time
from pathlib import Path

from aleph_zero import memstat
from aleph_zero.indexer import Index
from aleph_zero.search import search
from aleph_zero.store import db
from aleph_zero.view import build_view

KINDS = ("function", "method", "class", "interface", "enum", "type")


def check(repo: Path, sample: int, seed: int = 7) -> dict:
    index_dir = db.index_path(repo).parent
    shutil.rmtree(index_dir, ignore_errors=True)  # cold
    view = build_view(repo)
    conn = db.open_db(db.index_path(repo))
    index = Index(conn)
    started = time.perf_counter()
    report = index.sync(view, budget_seconds=3600)
    seconds = time.perf_counter() - started
    counts = index.counts()
    families = dict(conn.execute(
        "SELECT family, count(*) FROM blobs WHERE kind NOT LIKE 'skip:%' GROUP BY family ORDER BY 2 DESC"))
    marks = ",".join("?" * len(KINDS))
    unique = conn.execute(
        f"SELECT s.name, s.blob_oid, s.family, s.line, s.kind FROM symbols s WHERE s.kind IN ({marks}) "
        "AND length(s.name) >= 4 AND s.name_key IN (SELECT name_key FROM symbols GROUP BY name_key HAVING count(*) = 1)",
        KINDS).fetchall()
    random.Random(seed).shuffle(unique)
    ranks: dict[str, list[int]] = {}
    latency: list[float] = []
    misses: list[str] = []
    for name, oid, family, line, _kind in unique[:sample]:
        paths = set(index.paths_for(oid, family))
        begun = time.perf_counter()
        hits = search(index, view, name, k=10)
        latency.append((time.perf_counter() - begun) * 1000)
        rank = next((position for position, hit in enumerate(hits, 1)
                     if hit.path in paths and hit.scope_start_line <= line <= hit.scope_end_line), 0)
        ranks.setdefault(family, []).append(rank)
        if rank != 1 and len(misses) < 8:
            misses.append(f"{name} ({family}) rank {rank or '>10'}; first: "
                          + (f"{hits[0].path}:{hits[0].start_line} {hits[0].kind}" if hits else "nothing"))
    return {
        "repo": repo.name, "files_in_view": len(view.files), "indexed": report.indexed, "families": families,
        "chunks": counts["chunks_in_view"], "symbols": counts["symbols_in_view"], "imports": counts["imports_in_view"],
        "cold_index_seconds": round(seconds, 2), "private_mb": round(memstat.private_bytes() / 1e6, 1),
        "name_queries": {
            family: {"asked": len(found), "first": sum(rank == 1 for rank in found),
                     "top3": sum(0 < rank <= 3 for rank in found), "missing": sum(rank == 0 for rank in found)}
            for family, found in sorted(ranks.items())
        },
        "search_ms_p50": round(statistics.median(latency), 2) if latency else None,
        "search_ms_max": round(max(latency), 2) if latency else None,
        "examples_not_first": misses,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repos", nargs="+", type=Path)
    parser.add_argument("--sample", type=int, default=200)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    results = [check(repo.resolve(), args.sample) for repo in args.repos]
    text = json.dumps(results, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
