"""Would an out-of-date compiler index still be worth showing, marked as such?

Usage:
  uv run python bench/eval/scip_stale.py bench/data/repos/flask --back 10 50 200 [--out FILE]

ADR-004 believes an imported index for a file only while that file and everything it depends on
are unchanged. The question measured here, fixed in the M9 plan before it was run: for the calls
that rule sends back to the resolver although their own file has not changed, what would be gained
and what risked by showing the stale index's answer under a weaker stamp?

For each distance N: an index is made at HEAD~N (first parent) and imported into HEAD stamped
with that commit; a fresh index of HEAD is made beside it. Over the stored calls of files that are
the same as at HEAD~N but not believed (a dependency changed): how many the resolver answers
certainly; of the others, how many the stale index answers as the fresh one does (same file, same
name, and the same line where the target file is unchanged), and how many differently.

Registered rule: the weaker stamp is built if what it would add rightly is at least 10% of those
calls, summed over the distances, and what it would show differently is under 2% of what it would
show. Works on temporary clones; needs scip-python; runs offline.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from aleph_zero import callgraph, gitio, indexers, scipstore
from aleph_zero.engine import Engine


def clone_at(source: Path, where: Path, revision: str) -> Path:
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(source), str(where)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(where), "checkout", "-q", "--detach", revision], check=True, capture_output=True)
    return where


def make_index(root: Path, out: Path) -> dict:
    return indexers.run(root, "python", out)


def prepared(root: Path) -> tuple:
    engine = Engine(root, dense=False)
    view = engine._view(None)
    index = engine._index(view)
    index.sync(view, budget_seconds=600)
    callgraph.prepare(index, view, 600)
    return engine, view, index, callgraph.load(index, view), scipstore.Compiled(index.conn, view.files)


def compare(source: Path, head: str, back: int, scratch: Path, fresh_index: Path) -> dict:
    old = subprocess.run(["git", "-C", str(source), "rev-parse", f"{head}~{back}"], capture_output=True, text=True, check=True).stdout.strip()
    stale_index = scratch / f"stale-{back}.scip"
    make_index(clone_at(source, scratch / f"old-{back}", old), stale_index)
    with_stale, with_fresh = clone_at(source, scratch / f"head-stale-{back}", head), clone_at(source, scratch / f"head-fresh-{back}", head)
    Engine(with_stale, dense=False).scip_import(stale_index, tree=old)
    Engine(with_fresh, dense=False).scip_import(fresh_index, tree=head)
    _, view, index, graph, stale = prepared(with_stale)
    _, _, _, fresh_graph, fresh = prepared(with_fresh)
    then = gitio.read_tree(with_stale, old)[0]
    counts: Counter = Counter()
    for path in sorted(graph.files):
        state = stale.status(path)
        if state == "" or state is None:
            counts["files believed" if state == "" else "files the old index does not cover"] += 1
            continue
        if then.get(path) != view.files[path]:
            counts["files changed themselves"] += 1
            continue
        counts["files unchanged but not believed"] += 1
        of_file = graph.files.get(path)
        for call in callgraph._calls(index, (view.files[path], of_file.family)):
            counts["calls in them"] += 1
            edge = graph.resolve(path, call)
            if edge is not None and edge.certain:
                counts["answered certainly by the resolver"] += 1
                continue
            where = (path, call.line - 1, (call.column, call.column16, call.column32), call.name)
            said, now = stale.target(*where), fresh.target(*where)
            if not said or callgraph._defined_at(graph, *said) is None:
                counts["the stale index has nothing to show"] += 1
                continue
            same_place = said[1] == now[1] if now and then.get(said[0]) == view.files.get(said[0]) else True
            if now and now[0] == said[0] and now[2] == said[2] and same_place:
                counts["the stale index would add, rightly"] += 1
            else:
                counts["the stale index would show something the fresh one does not"] += 1
    return {"back": back, "old": old[:12], **dict(counts)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("repository", type=Path)
    parser.add_argument("--back", type=int, nargs="+", default=[10, 50, 200])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    head = subprocess.run(["git", "-C", str(args.repository), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    rows = []
    with tempfile.TemporaryDirectory(prefix="aleph-stale-") as scratch:
        fresh_index = Path(scratch) / "fresh.scip"
        made = make_index(clone_at(args.repository, Path(scratch) / "head", head), fresh_index)
        for back in args.back:
            rows.append(compare(args.repository, head, back, Path(scratch), fresh_index))
            print(json.dumps(rows[-1]), flush=True)
    total = Counter()
    for row in rows:
        total.update({key: value for key, value in row.items() if isinstance(value, int) and key != "back"})
    downgraded, adds, differs = total["calls in them"], total["the stale index would add, rightly"], total["the stale index would show something the fresh one does not"]
    verdict = {"downgraded_calls": downgraded, "would_add_rightly": adds, "would_show_differently": differs,
               "added_share": round(adds / downgraded, 4) if downgraded else 0.0,
               "differing_share_of_shown": round(differs / (adds + differs), 4) if adds + differs else 0.0}
    verdict["build_it"] = verdict["added_share"] >= 0.10 and verdict["differing_share_of_shown"] < 0.02
    report = {"repository": str(args.repository), "head": head[:12], "indexer": made["indexer"], "distances": rows, "sum": verdict}
    print(json.dumps(verdict, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
