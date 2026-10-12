"""Command line: aleph index | search | symbol | outline | callers | callees | status | gc."""
from __future__ import annotations

import argparse
import json
import time
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import callgraph, doctor, gitio, indexers, outline, render, scipstore, sources
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
    engine = Engine(args.cwd, dense=_dense_setting(args))
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


def _references(args) -> int:
    found = Engine(args.cwd).references(args.name, direction=args.command, k=args.k, timeout_ms=args.timeout_ms)
    if args.json:
        print(json.dumps(asdict(found), indent=2))
        return 0
    print(callgraph.text(found))
    return 0


def _scip(args) -> int:
    engine = Engine(args.cwd, dense=False)
    if args.action == "import":
        try:
            kept = engine.scip_import(args.file, tree=args.tree, worktree=args.worktree)
        except (scipstore.NotAnIndex, scipstore.Unstamped) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except gitio.GitError as exc:
            print(f"cannot read the tree to stamp the index with: {exc}", file=sys.stderr)
            return 1
        print(f"kept {kept['documents']} documents ({kept['references']} references) of {kept['tool'] or 'an unnamed indexer'}, "
              f"stamped with {kept['stamped']}" + (f"; {kept['skipped']} left out (not in that tree)" if kept["skipped"] else ""))
        return 0
    if args.action == "run":
        try:
            report = engine.scip_run(args.lang)
        except (indexers.CannotRun, scipstore.NotAnIndex) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        kept = report["imported"]
        print(f"{report['indexer']} ran {report['seconds']} s, peak {report['peak_mb']} MB of {report['limit_mb']:.0f} MB allowed; "
              f"kept {kept['documents']} documents ({kept['references']} references), stamped with the worktree as it is")
        return 0
    if args.action == "list":
        listed = engine.scip_imports()
        if args.json:
            print(json.dumps(listed, indent=2))
            return 0
        for row in listed:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["imported_at"]))
            print(f"{row['import_id']}  {when}  {row['tool'] or '?':16} {row['documents']} documents, stamped with {row['stamped']}")
        return 0
    if args.import_id is None and not args.all:
        print("say which import to forget (see `aleph scip list`), or --all", file=sys.stderr)
        return 2
    print(f"forgot {engine.scip_forget(None if args.all else args.import_id)} documents")
    return 0


def _outline(args) -> int:
    result = Engine(args.cwd).outline(args.path)
    print(json.dumps(asdict(result), indent=2) if args.json else outline.text(result))
    return 0


def _status(args) -> int:
    print(json.dumps(Engine(args.cwd).status(), indent=2))
    return 0


def _dense_setting(args) -> bool | None:
    """The owner's choice about search by meaning: a flag, else ALEPH_DENSE=1 or 0, else
    None, which lets the benchmark verdict for the project's main language decide."""
    if getattr(args, "dense", False):
        return True
    if getattr(args, "no_dense", False):
        return False
    return {"1": True, "0": False}.get(os.environ.get("ALEPH_DENSE", ""))


def _serve(args) -> int:
    from . import server  # only this command needs it

    return server.main(Engine(args.cwd, dense=_dense_setting(args)))


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


def _embed(args) -> int:
    from .embed import setup, worker

    if args.action == "setup":
        say = lambda text: print(text, flush=True)  # noqa: E731
        binary = setup.install_binary(args.llama_source, say)
        model = setup.install_model(args.model_file, say)
        print(f"installed {binary}\ninstalled {model}")
    elif args.action == "stop":
        worker.stop()
        print("stopped")
    elif args.action == "run":
        say = lambda done, left: print(f"  {done} embedded", end="\r", flush=True)  # noqa: E731
        report = Engine(args.cwd, dense=True).embed(budget_seconds=args.minutes * 60, progress=say)
        print(f"embedded {report.embedded} chunks in {report.seconds:.0f} s; {report.remaining} chunks left"
              + (f" ({report.reason})" if report.reason else ""))
    else:
        known = worker.state()
        print(f"installed: {'yes' if setup.installed() else 'no'} ({worker.home()})")
        print(f"running: {'yes' if worker.available() else 'no'}"
              + (f" (worker pid {known['worker_pid']})" if known.get("worker_pid") and worker.available() else ""))
    return 0


def _memory(args) -> int:
    engine = Engine(args.cwd, dense=False)
    if args.action == "list":
        for row in engine.memories(args.limit):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["created_at"]))
            print(f"{row['memory_id']}  {when}  {row['kind']:5} {' '.join(row['text'].split())[:100]}")
        return 0
    if args.action == "recall":
        found = engine.recall(" ".join(args.query), k=args.k)
        if args.json:
            from dataclasses import asdict

            print(json.dumps([asdict(item) for item in found]))
        else:
            from .server import recalled_text

            print(recalled_text(found, " ".join(args.query)))
        return 0
    if bool(args.all) == bool(args.memory_id):
        print("aleph: name one memory to forget (its id is in `aleph memory list`), or pass --all by itself", file=sys.stderr)
        return 2
    forgotten = engine.forget(None if args.all else args.memory_id)
    print(f"forgot {forgotten}")
    return 0 if forgotten or args.all else 1


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
    meaning = find.add_mutually_exclusive_group()
    meaning.add_argument("--dense", action="store_true", help="also search by meaning (needs `aleph embed run` first)")
    meaning.add_argument("--no-dense", action="store_true", help="search by words and names only")
    find.add_argument("--json", action="store_true")
    find.set_defaults(run=_search)

    symbol = commands.add_parser("symbol", help="where a name is defined in the current view")
    symbol.add_argument("name", help="a name, or a qualified one: TokenStore.get, auth::TokenStore")
    symbol.add_argument("-k", type=int, default=20)
    symbol.add_argument("--timeout-ms", type=int)
    symbol.add_argument("--json", action="store_true")
    symbol.set_defaults(run=_symbol)

    for direction, what in (("callers", "who calls a definition"), ("callees", "what a definition calls")):
        graph = commands.add_parser(direction, help=f"{what}, and how sure each answer is")
        graph.add_argument("name", help="a name, or a qualified one: TokenStore.get")
        graph.add_argument("-k", type=int, default=50)
        graph.add_argument("--timeout-ms", type=int)
        graph.add_argument("--json", action="store_true")
        graph.set_defaults(run=_references)

    compiled = commands.add_parser("scip", help="compiler-grade indexes (SCIP) made elsewhere: exact answers while their files are unchanged")
    scip_actions = compiled.add_subparsers(dest="action", required=True)
    importing = scip_actions.add_parser("import", help="keep an index; say what it was made from")
    importing.add_argument("file", type=Path)
    made_from = importing.add_mutually_exclusive_group()
    made_from.add_argument("--tree", help="the revision the index was made from (default: HEAD)")
    made_from.add_argument("--worktree", action="store_true", help="it was made from the worktree as it is now, uncommitted changes included")
    running = scip_actions.add_parser("run", help="run this language's indexer here, inside the memory limit, and keep the result")
    running.add_argument("--lang", help="python, java, go or rust (the languages measured under the limit); default: the worktree's main language")
    running.add_argument("--json", action="store_true")
    scip_actions.add_parser("list").add_argument("--json", action="store_true")
    forgetting = scip_actions.add_parser("forget")
    forgetting.add_argument("import_id", nargs="?", type=int)
    forgetting.add_argument("--all", action="store_true")
    compiled.set_defaults(run=_scip)

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

    embed = commands.add_parser("embed", help="the embedding worker: install, status, stop")
    embedding = embed.add_subparsers(dest="action", required=True)
    installing = embedding.add_parser("setup", help="build the worker and download the model (uses the network)")
    installing.add_argument("--llama-source", type=Path, help="an existing llama.cpp checkout to build from")
    installing.add_argument("--model-file", type=Path, help="a local copy of the model file, instead of downloading")
    running = embedding.add_parser("run", help="make vectors for this worktree's chunks (about a hundred a second)")
    running.add_argument("--minutes", type=float, default=float("inf"), help="stop after this long; it resumes next time")
    embedding.add_parser("status")
    embedding.add_parser("stop")
    embed.set_defaults(run=_embed)

    remembered = commands.add_parser("memory", help="what earlier sessions left about this repository: list, recall, forget")
    memories = remembered.add_subparsers(dest="action", required=True)
    listing = memories.add_parser("list")
    listing.add_argument("--limit", type=int, default=50)
    recalling = memories.add_parser("recall")
    recalling.add_argument("query", nargs="+")
    recalling.add_argument("-k", type=int, default=5)
    recalling.add_argument("--json", action="store_true")
    forgetting = memories.add_parser("forget", help="remove a memory from Aleph's catalogue so it is never recalled again; --all for every one. With Aeon as the vector store, a copy of the text stays in Aeon's file (ADR-006)")
    forgetting.add_argument("memory_id", nargs="?")
    forgetting.add_argument("--all", action="store_true")
    remembered.set_defaults(run=_memory)

    gc = commands.add_parser("gc", help="drop content no view has used recently")
    gc.add_argument("--days", type=float, default=30.0)
    gc.set_defaults(run=_gc)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except (gitio.GitError, StoreError, outline.OutlineError, ValueError, RuntimeError) as exc:
        print(f"aleph: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
