"""Entering the knowledge graph by meaning (DEFERRED entry 0-graph, part 6; rules written before any run).

`questions`: part 2's route questions asked again by their OpenAPI operation's summary, method and path left out, the
same answering files; a summary holding a path, or an identifier written as code that a gold file's name spells, is
left out. `ceiling`: search on the summary, and the answering files its ten miss that the graph reaches from its first
five results: the definitions they lie in, the routes those definitions handle, and those routes' registering lines,
handlers and mounts.

    facts_meaning.py questions SRC_DIR OUT.json
    facts_meaning.py ceiling WORKDIR --questions FILE --out FILE
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402
from facts_bench import SHIPS_WITH_VECTORS, _ten, draw_of  # noqa: E402
from facts_questions import PROJECTS  # noqa: E402

from aleph_zero import archfacts  # noqa: E402
from aleph_zero.embed import worker  # noqa: E402
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402
from aleph_zero.vectors import VectorStore  # noqa: E402

CODE_WORD = re.compile(r"\b(\w*_\w+|[a-z]+[A-Z]\w*|[A-Z][a-z]+[A-Z]\w*)\b")
DECORATOR_LINES = 4      # a decorated handler: its route is stated this many lines above its definition at most


def questions(source: Path, src: Path) -> list[dict]:
    out = []
    for row in json.loads(source.read_text()):
        if row["category"] != "route":
            continue
        spec = PROJECTS[row["repo"]][1]
        text = (src / row["repo"].replace("/", "_") / spec).read_text()
        document = json.loads(text) if spec.endswith(".json") else yaml.safe_load(text)
        operation = (document["paths"].get(row["name"]) or {}).get(row["method"].lower()) or {}
        summary = (operation.get("summary") or "").strip() or (operation.get("description") or "").strip().split("\n")[0]
        stems = {Path(path).stem.lower() for path in row["gold"]}
        if not summary or "/" in summary or any(word.lower() in stems for word in CODE_WORD.findall(summary)):
            continue
        out.append({**row, "instance_id": row["instance_id"].replace("-route-", "-meaning-"), "problem_statement": summary})
    return out


def _handled_at(index, view, routes) -> dict:
    """Each route's handler, as (path, line): a named handler's definition, or for a decorated one the route's own line."""
    where = {}
    for found in archfacts.handlers(index, view, routes):
        where.setdefault(found.name, []).append((found.path, found.line))
    for route in routes:
        if route.name not in where and " " not in route.detail:
            where[route.name] = [(route.path, route.line)]
    return where


def score_row(row, workdir: Path, client) -> dict | None:
    try:
        view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
    except Exception:  # noqa: BLE001
        return None
    index = Index(open_db(index_file(view.common_dir)))
    try:
        if index.sync(view, budget_seconds=0.0).pending:
            return None
        while archfacts.prepare(index, view, 600.0):
            pass
        nearest = None
        if row["language"] in SHIPS_WITH_VECTORS and client is not None:
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            have, total = store.coverage(index)
            if total and have == total:
                vector = client.embed([row["problem_statement"]], kind="query")[0]
                nearest = lambda limit, scoped: [c for c, _ in store.search(index, vector, limit, scoped)]  # noqa: E731
        hits = bench.search(index, view, row["problem_statement"], k=bench.DEEP, dense=nearest)
        searched = _ten(hit.path for hit in hits)
        top = [hit for hit in hits if hit.path in searched[:5]]
        found = archfacts.load(index)
        routes = [each for each in found if each.kind == "route" and not archfacts.NEVER_AN_ANSWER.search(each.path)]
        handled = _handled_at(index, view, routes)
        reached: list[str] = []
        for hit in top:
            first = (hit.scope_start_line or hit.start_line) - DECORATOR_LINES
            last = hit.scope_end_line or hit.end_line
            for route in routes:
                if any(path == hit.path and first <= line <= last for path, line in handled.get(route.name, [])):
                    files = [route.path, *(path for path, _ in handled.get(route.name, []))]
                    files += [each.path for each in archfacts.matching(found, [archfacts.Mention("route", route.name)])]
                    reached += [path for path in files if path not in reached]
        gold = set(row["gold"])
        return {"instance": row["instance_id"], "repo": row["repo"], "draw": draw_of(row["instance_id"]), "gold": sorted(gold),
                "question": row["problem_statement"], "search": searched, "graph": reached,
                "recall": len(set(searched) & gold) / len(gold),
                "mrr": next((1 / place for place, path in enumerate(searched, 1) if path in gold), 0.0),
                "beyond": sorted((gold & set(reached)) - set(searched)), "routes_reached": len(reached) > 0}
    finally:
        index.conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["questions", "ceiling"])
    parser.add_argument("where", type=Path)
    parser.add_argument("out_or_none", type=Path, nargs="?")
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.action == "questions":
        made = questions(Path(__file__).with_name("facts_questions.json"), args.where)
        args.out_or_none.write_text(json.dumps(made, indent=1))
        print(len(made), "questions")
        return 0
    rows = json.loads(args.questions.read_text())
    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60) \
        if any(row["language"] in SHIPS_WITH_VECTORS for row in rows) else None
    results = []
    for number, row in enumerate(rows, 1):
        entry = score_row(row, args.where.resolve(), client)
        if entry is None:
            print(f"{number:4}/{len(rows)} {row['instance_id']}: not prepared, left out", file=sys.stderr, flush=True)
            continue
        results.append(entry)
        print(f"{number:4}/{len(rows)} {row['instance_id']:32} search {entry['recall']:.2f} beyond +{len(entry['beyond'])}",
              file=sys.stderr, flush=True)
    args.out.write_text(json.dumps(results, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
