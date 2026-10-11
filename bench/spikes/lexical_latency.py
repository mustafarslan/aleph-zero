"""Latency of the lexical pipeline on a real repository: view, sync, and search.

Usage: uv run python bench/spikes/lexical_latency.py REPO [--runs N] [--out FILE]

The M0 storage spike used a uniform synthetic vocabulary. This one uses a real index,
and includes the queries that hurt most on real code: words that match most chunks.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from aleph_zero import lexical, memstat
from aleph_zero.indexer import Index
from aleph_zero.search import search
from aleph_zero.store import db
from aleph_zero.view import build_view

TYPICAL = [
    "QuerySet filter", "get_or_create", "migration autodetector", "csrf token middleware",
    "where is the session cookie set", "render_to_response", "ModelAdmin get_queryset",
    "password hasher", "url resolver reverse", "database connection pool",
    "how are signals dispatched", "TemplateSyntaxError", "cache backend redis", "file upload handler",
]
# Words in a large share of all chunks: the posting lists are as long as they get.
COMMON = ["self", "return", "self return", "def self return None", "import from", "test", "if not self"]


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def timed(fn, runs: int) -> list[float]:
    out = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


def summary(values: list[float]) -> dict[str, float]:
    return {
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms": round(percentile(values, 0.95), 2),
        "max_ms": round(max(values), 2),
    }


def measure(repo: Path, runs: int) -> dict:
    view_ms = timed(lambda: build_view(repo), max(5, runs // 2))
    view = build_view(repo)
    conn = db.open_db(db.index_path(repo))
    index = Index(conn)
    start = time.perf_counter()
    report = index.sync(view, budget_seconds=600)
    sync_seconds = time.perf_counter() - start
    result = {
        "repo": str(repo),
        "files": len(view.files),
        "counts": index.counts(),
        "sync": {"indexed": report.indexed, "pending": report.pending, "seconds": round(sync_seconds, 2)},
        "view_build": summary(view_ms),
        "queries": {},
    }
    for label, queries in (("typical", TYPICAL), ("common_terms", COMMON)):
        fts, full, slowest = [], [], ("", 0.0)
        for query in queries:
            terms = lexical.query_terms(query)
            if terms:
                fts += timed(lambda: lexical.search(conn, query, 50), runs)
            times = timed(lambda: search(index, view, query, k=8), runs)
            full += times
            if statistics.median(times) > slowest[1]:
                slowest = (query, round(statistics.median(times), 2))
        result["queries"][label] = {"fts_only": summary(fts), "search": summary(full), "slowest": slowest}
    result["private_mb"] = round(memstat.private_bytes() / 1e6, 1)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repo", type=Path)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = measure(args.repo.resolve(), args.runs)
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        args.out.write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
