"""Command line: aleph index | search | status | gc."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import gitio
from .indexer import Index, collect_garbage
from .search import search
from .store import StoreError, index_file, open_db
from .view import build_view


def _open(cwd: Path) -> tuple[Index, object]:
    view = build_view(cwd)
    return Index(open_db(index_file(view.common_dir))), view


def _index(args) -> int:
    index, view = _open(args.cwd)
    indexed = skipped = 0
    while True:
        report = index.sync(view, budget_seconds=args.budget)
        indexed += report.indexed
        skipped += report.skipped
        if report.pending == 0 or args.once:
            break
        fresh = build_view(args.cwd)  # the worktree may have moved on while we indexed
        if report.indexed + report.skipped == 0 and fresh.fingerprint == view.fingerprint:
            break  # nothing more can be done right now
        view = fresh
    print(f"indexed {indexed} files, skipped {skipped}, pending {report.pending}; "
          f"{index.counts()['chunks_in_view']} chunks in view")
    return 0


def _search(args) -> int:
    index, view = _open(args.cwd)
    report = index.sync(view)
    hits = search(index, view, args.query, k=args.k, kind=args.kind, path_glob=args.glob)
    if args.json:
        print(json.dumps({"pending": report.pending, "hits": [asdict(hit) for hit in hits]}, indent=2))
        return 0
    for hit in hits:
        label = hit.breadcrumb or hit.symbol
        print(f"{hit.path}:{hit.start_line}-{hit.end_line}  {hit.kind}" + (f"  {label}" if label else ""))
        first = next((line.strip() for line in hit.text.splitlines() if line.strip()), "")
        print(f"    {first[:160]}")
    if report.pending:
        print(f"note: {report.pending} files are not indexed yet; run `aleph index`", file=sys.stderr)
    return 0


def _status(args) -> int:
    index, view = _open(args.cwd)
    report = index.sync(view, budget_seconds=0.0)
    counts = index.counts()
    counts.update({"root": str(view.root), "files_in_view": len(view.files), "dirty": len(view.dirty),
                   "pending": report.pending, "index": str(index_file(view.common_dir))})
    print(json.dumps(counts, indent=2))
    return 0


def _gc(args) -> int:
    index, view = _open(args.cwd)
    index.sync(view, budget_seconds=0.0)
    print(json.dumps(collect_garbage(index.conn, args.days)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aleph", description="Local code search for coding agents.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="a directory inside the repository")
    commands = parser.add_subparsers(dest="command", required=True)

    index = commands.add_parser("index", help="index everything in the current view")
    index.add_argument("--budget", type=float, default=30.0, help="seconds per pass")
    index.add_argument("--once", action="store_true", help="stop after one pass even if files remain")
    index.set_defaults(run=_index)

    find = commands.add_parser("search", help="search the current view")
    find.add_argument("query")
    find.add_argument("-k", type=int, default=8)
    find.add_argument("--kind", choices=("any", "code", "docs"), default="any")
    find.add_argument("--glob", help="only paths matching this pattern")
    find.add_argument("--json", action="store_true")
    find.set_defaults(run=_search)

    status = commands.add_parser("status", help="what the index holds for this worktree")
    status.set_defaults(run=_status)

    gc = commands.add_parser("gc", help="drop content no view has used recently")
    gc.add_argument("--days", type=float, default=30.0)
    gc.set_defaults(run=_gc)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (gitio.GitError, StoreError) as exc:
        print(f"aleph: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
