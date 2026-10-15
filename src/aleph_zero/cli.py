"""Command line: aleph index | search | symbol | outline | callers | callees | status | gc."""
from __future__ import annotations

import argparse
import json
import time
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import archfacts, callgraph, decisions, doctor, gitio, indexers, outline, plain, render, scipstore, sources, vectorfile
from .engine import Engine
from .indexer import PREPARE_PROCESSES, Index, collect_garbage
from .store import StoreError, index_file, open_db
from .view import build_view
from .names import COMMAND


def _open(cwd: Path) -> tuple[Index, object]:
    view = build_view(cwd)
    return Index(open_db(index_file(view.common_dir))), view


def _index(args) -> int:
    index, view = _open(args.cwd)
    index.workers = PREPARE_PROCESSES       # a person waits for this one: the files are prepared on several cores
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
    render.NARROWER = "narrow the query, add --glob, or ask for --detail locations"
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
    if not found.symbols:
        print(f'no definition named "{args.name}" in this worktree')
    if found.pending:
        print(f"note: {found.pending} files are not indexed yet; run `{COMMAND} index`", file=sys.stderr)
    return 0


def _references(args) -> int:
    found = Engine(args.cwd).references(args.name, direction=args.command, k=args.k, timeout_ms=args.timeout_ms)
    if args.json:
        print(json.dumps(asdict(found), indent=2))
        return 0
    print(callgraph.text(found))
    return 0


def _facts(args) -> int:
    found = Engine(args.cwd).facts(args.kind, like=args.like, k=args.k, timeout_ms=args.timeout_ms)
    print(json.dumps(asdict(found), indent=2) if args.json else archfacts.text(found))
    return 0


def _decisions(args) -> int:
    found = Engine(args.cwd).decisions(" ".join(args.task), k=args.k, path=args.path, timeout_ms=args.timeout_ms)
    print(json.dumps(asdict(found), indent=2) if args.json else decisions.text(found))
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
        if not listed:
            print(f"no SCIP indexes imported here (`{COMMAND} scip run` or `{COMMAND} scip import FILE`)")
        for row in listed:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["imported_at"]))
            print(f"{row['import_id']}  {when}  {row['tool'] or '?':16} {row['documents']} documents, stamped with {row['stamped']}")
        return 0
    if args.import_id is None and not args.all:
        print(f"say which import to forget (see `{COMMAND} scip list`), or --all", file=sys.stderr)
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
    try:
        gitio.repo_info(args.cwd)
    except gitio.NoRepository:
        plain.unreadable_repository(args.cwd)       # a damaged `.git` is said to be that, not "no repository"
        raise gitio.NotARepository(f"{args.cwd} is not a git repository: other repositories are linked to a repository, "
                                   f"not to a directory. Run `git init` there") from None
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
        if args.model_file is not None and not Path(args.model_file).is_file():
            raise ValueError(f"{args.model_file} is not a file: --model-file names a copy of {worker.MODEL_FILE} on this machine")
        say = lambda text: print(text, flush=True)  # noqa: E731
        with setup.installing(say):
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


def _setup(args) -> int:
    """The checklist of ADR-013: shown to anyone, asked on a terminal, done one flag at a time otherwise."""
    from . import firstrun, settings
    from .embed import setup as embed_setup

    if args.embed and args.no_dense:
        raise SystemExit("aleph setup: --embed makes this repository's vectors in order to use them; it does not go with --no-dense")
    # With --json standard output is the list and nothing else: what was done is said beside it.
    say = lambda text: print(text, file=sys.stderr if args.json else sys.stdout, flush=True)  # noqa: E731
    failed: list[str] = []

    def now():
        index, view = _open(args.cwd)                           # the list needs the counts, also of what was just linked;
        index.workers = PREPARE_PROCESSES
        while index.sync(view, budget_seconds=30.0).pending:    # indexing costs seconds and nothing else
            fresh = build_view(args.cwd)
            if fresh.fingerprint == view.fingerprint:
                break
            view = fresh
        index.conn.close()
        status = Engine(args.cwd).status()
        found = firstrun.look(status)
        kept = settings.read(Path(status["index"]).parent / "settings.json")
        return status, found, firstrun.plan(found, kept, settings.read(settings.machine_file()))

    def act(what: str, *given: str) -> str:
        if what == "worker":
            with embed_setup.installing(say):
                return f"installed {embed_setup.install_binary(None, say)} and {embed_setup.install_model(None, say)}"
        if what == "source":
            added = sources.add(args.cwd, given[0])
            return f"linked {added.name} ({added.kind}): {added.location}"
        if what == "untracked_cache":
            gitio.git(gitio.repo_info(args.cwd).root, "update-index", "--untracked-cache")
            return "git's untracked cache is on for this worktree (in its index; no configuration was changed)"
        try:                                                    # 'embed': in the foreground, and it can be left
            report = Engine(args.cwd, dense=True).embed(progress=lambda done, left: print(f"  {done} embedded", end="\r", flush=True))
        except KeyboardInterrupt:
            return f"interrupted; `{COMMAND} embed run` continues where this stopped"
        return (f"embedded {report.embedded} chunks in {report.seconds:.0f} s; {report.remaining} left"
                + (f" ({report.reason}); `{COMMAND} embed run` continues" if report.reason else ""))

    def flag(name: str, do) -> None:
        """One flag's work. What fails is said and counted; the flags after it are still done."""
        try:
            do()
        except firstrun.FAILURES as problem:
            failed.append(name)
            print(f"aleph setup {name}: not done: {problem}", file=sys.stderr)

    # The worker is the machine's, so it is installed first and from wherever this runs: a new terminal starts in the
    # home directory, which is not walked, and that is where the README's first step runs `setup --worker`.
    if args.worker:
        flag("--worker", lambda: (settings.answer(settings.machine_file(), "worker", None), say(act("worker"))))
    project_flags = bool(args.embed or args.source or args.untracked_cache or args.dense or args.no_dense)
    if args.worker and not project_flags and not _in_repository(args.cwd):
        # Not a project (the home directory, ~/Downloads): nothing of it is walked for a list that is a project's.
        if args.json:
            print(json.dumps(firstrun.as_json([]), indent=2))
        else:
            say(f"\nThe rest of the list is a project's: run `{COMMAND} setup` in one.")
        return 2 if failed else 0
    status, found, steps = now()
    repo_file = Path(status["index"]).parent / "settings.json"
    flagged = bool(args.worker or args.embed or args.source or args.untracked_cache or args.dense or args.no_dense)

    def needs_repository(name: str, what: str) -> bool:
        if status.get("repository", True):
            return True
        failed.append(name)
        print(f"aleph setup {name}: {status['root']} is not a git repository: {what}. `git init` there makes it one",
              file=sys.stderr)
        return False

    for location in args.source or ():
        if needs_repository("--source", "other repositories are linked to a repository, not to a directory"):
            flag("--source", lambda location=location: (say(act("source", location)), settings.answer(repo_file, "sources", None)))
    if args.embed and not firstrun.look(Engine(args.cwd).status()).worker:
        failed.append("--embed")
        print("aleph setup --embed: the embedding worker is not installed, so no vectors can be made: "
              f"`{COMMAND} setup --worker` first (310 MB; uses the network)", file=sys.stderr)
    elif args.dense or args.no_dense or args.embed:
        settings.change(repo_file, dense=not args.no_dense)     # kept for the repository: vectors made are then used
        if args.embed:
            flag("--embed", lambda: say(act("embed")))
    if args.untracked_cache and needs_repository("--untracked-cache", "git's untracked cache is a repository's"):
        flag("--untracked-cache", lambda: (say(act("untracked_cache")), settings.answer(repo_file, "untracked_cache", None)))
    if flagged:
        settings.change(repo_file, hint_shown=True)             # whoever set something up here knows the command
    if not flagged and not args.json and firstrun.on_terminal():
        say(f"Setting up Aleph Zero for {status['root']}\n\n{firstrun.text(steps)}")
        menu = firstrun.Menu(status, say, input, act)
        try:
            for key in [step.key for step in steps]:
                status, found, steps = now()                    # an answer changes what the next question is
                step = next(step for step in steps if step.key == key)
                # Asked again: what was declined, and the one line that is done only on the person's word.
                asked = step.state == "open" or (args.again and (step.state == "declined" or (key == "claude" and step.state == "done")))
                if asked and not (key == "vectors" and not found.worker):
                    menu.step(step, found)
        except (KeyboardInterrupt, EOFError):
            say(f"\nleft as it is: `{COMMAND} setup` asks about the rest")
        settings.change(repo_file, hint_shown=True)
        say(f"\nAfter a pull, `{COMMAND} update` brings all of this up to date in one go, for what changed only; "
            f"`{COMMAND} update --print-hook` prints the lines that make git do it by itself.\n")
    status, found, steps = now()
    if args.json:
        print(json.dumps(firstrun.as_json(steps), indent=2))
        return 2 if failed else 0
    say(firstrun.text(steps))
    if firstrun.open_steps(steps) and not firstrun.on_terminal():
        say(f"\nAnswer these on a terminal with `{COMMAND} setup`, or one at a time: --worker, --source LOCATION, --embed, "
            "--dense or --no-dense, --untracked-cache. Nothing that costs is done without its own flag.")
    return 2 if failed else 0


def _update(args) -> int:
    """ADR-014: what changed is brought up to date in one go. For a person, a git hook or a schedule."""
    from . import update

    if args.print_hook:
        print(update.hook_lines())
        return 0
    report = update.run(args.cwd, fetch=not args.no_fetch, minutes=args.minutes, dense=_dense_setting(args))
    failed = report.get("failed") or []
    if args.json:
        print(json.dumps(report, indent=2))
    elif not args.quiet:
        print("\n".join(update.text(report)))
    elif failed:
        print("\n".join(f"aleph update: FAILED {problem}" for problem in failed), file=sys.stderr)
    return 2 if failed else 0


def _memory(args) -> int:
    engine = Engine(args.cwd, dense=False)
    if args.action == "list":
        rows = engine.memories(args.limit)
        if not rows:
            print("no memories of this repository yet: the side chat's answers and what an agent remembers are kept here")
        for row in rows:
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
        print(f"aleph: name one memory to forget (its id is in `{COMMAND} memory list`), or pass --all by itself", file=sys.stderr)
        return 2
    forgotten = engine.forget(None if args.all else args.memory_id)
    print(f"forgot {forgotten}")
    return 0 if forgotten or args.all else 1


def _gc(args) -> int:
    index, view = _open(args.cwd)
    index.sync(view, budget_seconds=0.0)
    report = collect_garbage(index.conn, args.days)
    # The rows of what was collected are free; the disk under them goes back to the file system (the file keeps its length).
    report["given_back"] = vectorfile.give_back(index.conn, index_file(view.common_dir).parent)
    print(json.dumps(report))
    return 0


# The commands after which the one line about `aleph setup` may be said (once, on a terminal). Not `serve`, whose
# standard error is Claude Code's log, and not the commands that are about setting up themselves.
HINTED = ("index", "search", "symbol", "callers", "callees", "decisions", "facts", "outline", "status")


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version
    try:
        return version("aleph-zero")
    except PackageNotFoundError:     # a source tree that was never installed
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=COMMAND, description="Local code search for coding agents.")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="a directory inside the repository")
    parser.add_argument("--version", action="version", version=f"aleph-zero {_version()}")
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
    meaning.add_argument("--dense", action="store_true", help=f"also search by meaning (needs `{COMMAND} embed run` first)")
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

    governing = commands.add_parser("decisions", help="the decision records (ADRs) that govern a task; with no task, all of them")
    governing.add_argument("task", nargs="*", help="what is being done, in plain words or as a commit message")
    governing.add_argument("--for", dest="path", metavar="PATH", help="a file: the records whose applies-to covers it, and its layer in oxn.yaml")
    governing.add_argument("-k", type=int, default=7)
    governing.add_argument("--timeout-ms", type=int, help="do not wait longer than this for the records' vectors; the answer is then by words")
    governing.add_argument("--json", action="store_true")
    governing.set_defaults(run=_decisions)

    stated = commands.add_parser("facts", help="routes, models, tables, environment names, tasks and services the code states; with no kind, how many of each")
    stated.add_argument("kind", nargs="?", choices=archfacts.KINDS)
    stated.add_argument("--like", help="only names containing this")
    stated.add_argument("-k", type=int, default=200)
    stated.add_argument("--timeout-ms", type=int)
    stated.add_argument("--json", action="store_true")
    stated.set_defaults(run=_facts)

    compiled = commands.add_parser("scip", help="compiler-grade indexes (SCIP) made elsewhere: exact answers while their files are unchanged")
    scip_actions = compiled.add_subparsers(dest="action", required=True)
    importing = scip_actions.add_parser("import", help="keep an index; say what it was made from")
    importing.add_argument("file", type=Path)
    made_from = importing.add_mutually_exclusive_group()
    made_from.add_argument("--tree", help="the revision the index was made from (default: HEAD)")
    made_from.add_argument("--worktree", action="store_true", help="it was made from the worktree as it is now, uncommitted changes included")
    running = scip_actions.add_parser("run", help="run this language's indexer here, inside the memory limit, and keep the result")
    running.add_argument("--lang", help="python, java, go, rust, typescript or javascript (each measured; javascript has a higher memory "
                              "limit of its own; ALEPH_SCIP_MEMORY_MB names another); default: the worktree's main language")
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

    first = commands.add_parser("setup", help="what can be set up here, asked once on a terminal (search works without any of it)")
    first.add_argument("--again", action="store_true", help="ask again about what was declined")
    first.add_argument("--json", action="store_true", help="the checklist as JSON; nothing is asked")
    first.add_argument("--worker", action="store_true", help="install the embedding worker and its model (310 MB; uses the network)")
    first.add_argument("--source", action="append", metavar="LOCATION", help="link a repository by path or URL (a URL uses the network)")
    first.add_argument("--embed", action="store_true", help="make this repository's vectors now and use them here")
    chosen = first.add_mutually_exclusive_group()
    chosen.add_argument("--dense", action="store_true", help="use search by meaning in this repository, whatever its language's verdict "
                                                             "(a running server then makes the vectors in the background; --embed makes them now)")
    chosen.add_argument("--no-dense", action="store_true", help="do not use search by meaning in this repository")
    first.add_argument("--untracked-cache", action="store_true", help="git's untracked cache, in this worktree's index (no configuration is changed)")
    first.set_defaults(run=_setup)

    fresh = commands.add_parser("update", help="bring this repository up to date in one go: fetched sources, the index, facts and vectors "
                                               "for what changed (for a git hook or a nightly schedule)")
    fresh.add_argument("--no-fetch", action="store_true", help="leave the network alone: sources linked by URL are not fetched")
    fresh.add_argument("--minutes", type=float, default=10.0, help="of embedding at most (default 10); what is left is made next time")
    fresh.add_argument("--quiet", action="store_true", help="say nothing unless a part failed")
    fresh.add_argument("--json", action="store_true", help="the report as data")
    fresh.add_argument("--print-hook", action="store_true", help="print the lines for git's post-merge and post-checkout hooks; nothing is installed")
    fresh.set_defaults(run=_update)

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

    _cwd_after_the_command_too(parser)
    args = parser.parse_args(argv)
    from . import firstrun
    from .lang import parse

    if args.command != "serve" and firstrun.hint_wanted():
        parse.announce = lambda names: print(f"aleph: fetching the grammar for {', '.join(names)} (once; uses the network)",
                                             file=sys.stderr, flush=True)
    try:
        done = args.run(args)
        if args.command not in ("doctor", "serve"):
            _grammar_notice()
        sys.stdout.flush()      # here, where a closed pipe is caught, not at exit, where Python reports it
        if args.command in HINTED:
            from . import firstrun

            if firstrun.hint_wanted():
                said = firstrun.hint(args.cwd)
                if said:
                    print(said, file=sys.stderr)
        return done
    except (gitio.GitError, StoreError, outline.OutlineError, ValueError, RuntimeError) as exc:
        print(f"aleph: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"\naleph: interrupted; what was done is kept, and the next `{COMMAND} {args.command}` continues from there",
              file=sys.stderr)
        return 130
    except BrokenPipeError:
        # The reader went away (`| head`): there is nothing to say, and nowhere to say it.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    except OSError as exc:
        print(f"aleph: {exc}", file=sys.stderr)
        return 2


def _in_repository(cwd: Path) -> bool:
    try:
        gitio.common_dir(cwd)
    except gitio.GitError:
        return False
    return True


def _cwd_after_the_command_too(parser: argparse.ArgumentParser) -> None:
    """`aleph search x --cwd DIR` as well as `aleph --cwd DIR search x`: each command also takes --cwd, which when
    given there replaces the one given before it."""
    pending = [parser]
    while pending:
        for action in pending.pop()._actions:
            if isinstance(action, argparse._SubParsersAction):
                for command in action.choices.values():
                    if not any("--cwd" in a.option_strings for a in command._actions):
                        command.add_argument("--cwd", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                    pending.append(command)


def _grammar_notice() -> None:
    """On a terminal: a grammar that could not load means files searched by words only, without definitions, until
    it does (they are read again then). Said instead of leaving `symbol` and `outline` empty without a word."""
    from . import firstrun
    from .lang import parse

    failed = parse.failed()
    if failed and firstrun.hint_wanted():
        why = next(iter(failed.values()))
        print(f"aleph: the grammar for {', '.join(sorted(failed))} could not be loaded ({why[:160]}); those files are "
              f"searched by words only, without definitions, until it is. `{COMMAND} doctor --fetch-grammars` fetches it",
              file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
