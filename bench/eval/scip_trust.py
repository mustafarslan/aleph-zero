"""The trust rule on a real corpus: what an imported index answers, and what an edit sends back.

Usage:
  uv run python bench/eval/scip_trust.py bench/data/graph python-httpx httpx/_models.py [--out FILE]

The acceptance fixed in the M9 plan: with the corpus's index imported, calls it resolves are
answered from it; after one file is edited (not committed), every call in that file and in
every file that depends on it, directly or through others, comes from the resolver and says
why; the rest still come from the index. Works on a temporary clone; the corpus is not touched.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from aleph_zero import callgraph, scipstore
from aleph_zero.engine import Engine


def census(engine: Engine) -> tuple[Counter, dict[str, str]]:
    """How every stored call of the view is answered, and for each file why it is not answered by the index."""
    view = engine._view(None)
    index = engine._index(view)
    index.sync(view, budget_seconds=600)
    callgraph.prepare(index, view, 600)
    graph = callgraph.load(index, view)
    compiled = scipstore.Compiled(index.conn, view.files)
    counts: Counter = Counter()
    reasons: dict[str, str] = {}
    for path in sorted(graph.files):
        oid = view.files[path]
        of_file = graph.files.get(path)
        state = compiled.status(path)
        if state:
            reasons[path] = state
        for call in callgraph._calls(index, (oid, of_file.family)):
            edge, downgraded = callgraph.resolve(graph, compiled, path, call)
            kind = "no answer" if edge is None else "compiler index" if edge.level == "L2" else "resolver"
            where = "file not believed" if state else "file believed" if state == "" else "file not covered"
            if state == "" and downgraded:
                kind += ", the index points at something not recorded"
            counts[(kind, where)] += 1
            if edge is not None and edge.level == "L2":
                # "Answered with the index's target": the answer must be the definition that holds the line the index names.
                said = compiled.target(path, call.line - 1, (call.column, call.column16, call.column32), call.name)
                target = edge.targets[0]
                agrees = bool(said) and said[0] == target.path and target.start_line <= said[1] + 1 <= target.end_line
                counts[("of which the answer is the definition the index names" if agrees else "of which it is NOT", where)] += 1
    return counts, reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("corpus")
    parser.add_argument("edit", help="the file to edit, relative to the corpus")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="aleph-trust-") as scratch:
        copy = Path(scratch) / args.corpus
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(args.workdir / args.corpus), str(copy)], check=True, capture_output=True)
        engine = Engine(copy, dense=False)
        kept = engine.scip_import(args.workdir / f"{args.corpus}.scip")
        before, _ = census(engine)
        with (copy / args.edit).open("a") as handle:
            handle.write("\n# edited, not committed\n")
        after, reasons = census(Engine(copy, dense=False))
    show = lambda counts: {f"{kind}, {state}": number for (kind, state), number in sorted(counts.items())}  # noqa: E731
    direct = sum(1 for why in reasons.values() if f"depends on {args.edit}," in why)
    report = {"corpus": args.corpus, "imported": kept, "edited": args.edit, "before": show(before), "after": show(after),
              "files_not_believed_after": len(reasons),
              "of_which_the_file_itself": sum(1 for path in reasons if path == args.edit),
              "of_which_name_the_edited_file_as_the_reason": direct}
    print(json.dumps(report, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
