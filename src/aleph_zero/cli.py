"""Command line: aleph index | search | symbol | outline | status | gc."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import doctor, gitio, outline, render, sources
from .engine import Engine
from .indexer import Index, collect_garbage
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
    engine = Engine(args.cwd)
    answer = engine.search(args.query, k=args.k, kind=args.kind, path_glob=args.glob, lang=args.lang,
                           timeout_ms=args.timeout_ms)
    if args.json:
        print(json.dumps(render.as_json(answer), indent=2))
    else:
        print(engine.render(answer, detail=args.detail, budget_tokens=args.budget_tokens))
    return 0


def _symbol(args) -> int:
    found = Engine(args.cwd).symbol(args.name, k=args.k, timeout_ms=args.timeout_ms)
    if args.json:
        print(json.dumps(asdict(found), indent=2))
        return 0
    for hit in found.symbols:
        print(f"{hit.path}:{hit.line}  {hit.kind}  {hit.qualified}")
        print(f"    {hit.signature[:160]}")
    if found.pending:
        print(f"note: {found.pending} files are not indexed yet; run `aleph index`", file=sys.stderr)
    return 0


def _outline(args) -> int:
    result = Engine(args.cwd).outline(args.path)
    print(json.dumps(asdict(result), indent=2) if args.json else outline.text(result))
    return 0


def _status(args) -> int:
    print(json.dumps(Engine(args.cwd).status(), indent=2))
    return 0


def _serve(args) -> int:
    from . import server  # only this command needs it

    return server.main(Engine(args.cwd))


def _source(args) -> int:
    if args.action == "add":
        added = sources.add(args.cwd, args.location, name=args.name, ref=args.ref)
        print(f"added {added.name} ({added.kind}): {added.location}")
    elif args.action == "remove":
        sources.remove(args.cwd, args.name)
        print(f"removed {args.name}")
    elif args.action == "refresh":
        for name, outcome in sources.refresh(args.cwd, args.name).items():
            print(f"{name}: {outcome}")
    else:
        rows = sources.describe(gitio.common_dir(args.cwd))
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print(f"{len(rows)} linked sources")
            for row in rows:
                age = "" if row["age_seconds"] is None else f"  fetched {row['age_seconds']} s ago"
                problem = f"  PROBLEM: {row['last_error']}" if row["last_error"] else ""
                print(f"  {row['name']}  {row['kind']}  {row['location']}  {row['files']} files{age}{problem}")
    return 0


def _doctor(args) -> int:
    if args.fetch_grammars:
        from .lang import parse

        print(f"fetched grammars ({parse.ensure_grammars()} downloaded)")
    rows = doctor.check(args.cwd)
    print(doctor.text(rows))
    return 1 if any(row.level == "FAIL" for row in rows) else 0


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
    find.add_argument("--lang", help="only files in this language: python, ts, go, rust, java, cpp, markdown, ...")
    find.add_argument("--detail", choices=render.DETAILS, default="snippets")
    find.add_argument("--budget-tokens", type=int, default=render.DEFAULT_BUDGET, help="ceiling for the text answer")
    find.add_argument("--timeout-ms", type=int, help="answer within this time, marked partial if cut short")
    find.add_argument("--json", action="store_true")
    find.set_defaults(run=_search)

    symbol = commands.add_parser("symbol", help="where a name is defined in the current view")
    symbol.add_argument("name", help="a name, or a qualified one: TokenStore.get, auth::TokenStore")
    symbol.add_argument("-k", type=int, default=20)
    symbol.add_argument("--timeout-ms", type=int)
    symbol.add_argument("--json", action="store_true")
    symbol.set_defaults(run=_symbol)

    skeleton = commands.add_parser("outline", help="what a file defines, without reading it all")
    skeleton.add_argument("path", help="a path in this worktree, relative to its top")
    skeleton.add_argument("--json", action="store_true")
    skeleton.set_defaults(run=_outline)

    status = commands.add_parser("status", help="what the index holds for this worktree")
    status.set_defaults(run=_status)

    serve = commands.add_parser("serve", help="run the MCP server on standard input and output")
    serve.set_defaults(run=_serve)

    source = commands.add_parser("source", help="documentation or code that lives in another repository")
    actions = source.add_subparsers(dest="action", required=True)
    adding = actions.add_parser("add", help="link a repository by local path, or by URL (uses the network)")
    adding.add_argument("location")
    adding.add_argument("--name", help="how its paths are prefixed; default: the directory's name")
    adding.add_argument("--ref", help="URL sources: the branch or tag to read")
    listing = actions.add_parser("list")
    listing.add_argument("--json", action="store_true")
    actions.add_parser("remove").add_argument("name")
    actions.add_parser("refresh", help="fetch URL sources now").add_argument("name", nargs="?")
    source.set_defaults(run=_source)

    health = commands.add_parser("doctor", help="check that everything Aleph needs is in place")
    health.add_argument("--fetch-grammars", action="store_true", help="download missing grammars (uses the network)")
    health.set_defaults(run=_doctor)

    gc = commands.add_parser("gc", help="drop content no view has used recently")
    gc.add_argument("--days", type=float, default=30.0)
    gc.set_defaults(run=_gc)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (gitio.GitError, StoreError, outline.OutlineError, ValueError) as exc:
        print(f"aleph: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
