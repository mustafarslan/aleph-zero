"""Entering the knowledge graph by meaning (DEFERRED entry 0-graph, part 6; rules written before any run).

`questions`: part 2's route questions asked again by their OpenAPI operation's summary, method and path left out, the
same answering files; a summary holding a path, or an identifier written as code that a gold file's name spells, is
left out. `ceiling`: search on the summary, and the answering files its ten miss that the graph reaches from its first
five results: the definitions they lie in, the routes those definitions handle, and those routes' registering lines,
handlers and mounts.

Arms (`ceiling` scores them too), each ten files: `search`; `search_graph`, the graph's files from search's first five,
then search's; `route_words`, the three routes whose method, path and handler share most of the question's words, with
their handlers and mounts, then search's; `route_vectors`, the same by the cosine of the question with a vector made
for each route from those words.

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


STOP = frozenset("the a an of to for with and or by in on is are all from this that its it be".split())
_route_vectors: dict = {}      # (repository, commit) -> (routes, their vectors): made once a repository


def _words(text: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    found = {word for word in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if len(word) >= 2 and word not in STOP}
    return found | {word[:-1] for word in found if word.endswith("s") and len(word) > 3}     # users ~ user


def _route_text(route, handled) -> str:
    method, _, path = route.name.partition(" ")
    verbs = {"GET": "get return list show", "POST": "create add post", "PUT": "update replace set", "PATCH": "update change",
             "DELETE": "delete remove", "ANY": ""}
    handler = route.detail.partition(" ")[2]
    return " ".join([verbs.get(method, ""), " ".join(part for part in path.split("/") if part and not part.startswith("{")),
                     re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", handler.rsplit(".", 1)[-1])])


def _path_words(route) -> set[str]:
    return _words(" ".join(part for part in route.name.partition(" ")[2].split("/") if part and not part.startswith("{")))


def floored(routes, texts, asked: set[str]) -> list:
    """The routes a request may be about (part 6b's floor): two words or more in common, one of them a word of the path;
    best first."""
    scored = []
    for route in routes:
        shared = asked & _words(texts[route.name])
        if len(shared) >= 2 and shared & _path_words(route):
            scored.append((len(shared), route))
    return [route for _, route in sorted(scored, key=lambda pair: -pair[0])]


def floor_arms(row, routes, texts, handled, found, searched, client, asked) -> dict:
    """`route_words_floor` and `route_vectors_floor`: part 6's two arms with part 6b's floor."""
    passing = floored(routes, texts, asked)
    by_vectors = []
    if client is not None and passing:
        import numpy as np
        vectors = client.embed([texts[route.name] for route in passing], kind="document")
        question = client.embed([row["problem_statement"][:2000]], kind="query")[0]
        by_vectors = [passing[i] for i in np.argsort(-(vectors @ question))[:3]]
    return {"route_words_floor": _ten(_files_of_routes(passing[:3], handled, found) + searched),
            "route_vectors_floor": _ten(_files_of_routes(by_vectors, handled, found) + searched)}


def handover(index, view, found, text: str) -> list[str]:
    """The files `facts(mentioned=text, words=True)` hands over, in its order (part 13: the engine's own code)."""
    mentions = archfacts.mentioned(text)
    listed = archfacts.matching(found, mentions)
    if not any(mention.kind == "route" for mention in mentions):
        listed += [each for each in archfacts.with_mounts(found, archfacts.by_words(found, text)) if each not in listed]
    listed += archfacts.handlers(index, view, [each for each in listed if each.kind == "route"])
    return list(dict.fromkeys(each.path for each in listed))


def _files_of_routes(chosen, handled, found) -> list[str]:
    files: list[str] = []
    for route in chosen:
        for path in [route.path, *(path for path, _ in handled.get(route.name, []))] + \
                [each.path for each in archfacts.matching(found, [archfacts.Mention("route", route.name)])]:
            if path not in files:
                files.append(path)
    return files


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
        asked = _words(row["problem_statement"])
        texts = {route.name: _route_text(route, handled) for route in routes}
        by_words = sorted(((len(asked & _words(texts[route.name])), route) for route in routes), key=lambda pair: -pair[0])
        by_words = [route for score, route in by_words if score > 0][:3]
        by_vectors = []
        if client is not None and routes:
            import numpy as np
            key = (row["repo"], row["base_commit"])
            if key not in _route_vectors:
                _route_vectors[key] = (routes, client.embed([texts[route.name] for route in routes], kind="document"))
            listed, vectors = _route_vectors[key]
            question = client.embed([row["problem_statement"]], kind="query")[0]
            order = np.argsort(-(vectors @ question))[:3]
            by_vectors = [listed[i] for i in order]
        arms = {"search": searched, "search_graph": _ten(reached + searched),
                "route_words": _ten(_files_of_routes(by_words, handled, found) + searched),
                "route_vectors": _ten(_files_of_routes(by_vectors, handled, found) + searched),
                **floor_arms(row, routes, texts, handled, found, searched, client, asked),
                "handed_words": _ten(handover(index, view, found, row["problem_statement"]) + searched)}

        def mrr(files):
            return next((1 / place for place, path in enumerate(files, 1) if path in gold), 0.0)
        return {"instance": row["instance_id"], "repo": row["repo"], "draw": draw_of(row["instance_id"]), "gold": sorted(gold),
                "question": row["problem_statement"], "files": arms, "graph": reached,
                "recall": {name: len(set(files) & gold) / len(gold) for name, files in arms.items()},
                "mrr": {name: mrr(files) for name, files in arms.items()},
                "beyond": sorted((gold & set(reached)) - set(searched)), "routes_reached": len(reached) > 0}
    finally:
        index.conn.close()


def issue_row(row, workdir: Path, client, ships_vectors: bool) -> dict | None:
    """Part 6b (b): an issue people wrote; search on its first line as part 1 asks it, the floored arms on the whole issue."""
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
        question = bench.question_forms(index, row["problem_statement"])["title"]
        nearest = None
        if ships_vectors and client is not None:
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            have, total = store.coverage(index)
            if total and have == total:
                vector = client.embed([question], kind="query")[0]
                nearest = lambda limit, scoped: [c for c, _ in store.search(index, vector, limit, scoped)]  # noqa: E731
        searched = bench.first_files([hit.path for hit in bench.search(index, view, question, k=bench.DEEP, dense=nearest)], 10)
        found = archfacts.load(index)
        routes = [each for each in found if each.kind == "route" and not archfacts.NEVER_AN_ANSWER.search(each.path)]
        handled = _handled_at(index, view, routes)
        texts = {route.name: _route_text(route, handled) for route in routes}
        asked = _words(row["problem_statement"][:4000])
        arms = {"search": searched, **floor_arms(row, routes, texts, handled, found, searched, client, asked),
                "handed_words": _ten(handover(index, view, found, row["problem_statement"][:4000]) + searched)}
        gold = set(row["gold"])
        return {"instance": row["instance_id"], "repo": row["repo"], "language": row.get("language", "python"),
                "draw": draw_of(row["instance_id"]), "gold": sorted(gold), "routes": len(routes),
                "fired": {name: files[: len(files)] != searched for name, files in arms.items() if name != "search"},
                "recall": {name: len(set(files) & gold) / len(gold) for name, files in arms.items()}}
    finally:
        index.conn.close()


def issues(args) -> int:
    from graph_expand import GRADED, SHIPS_WITH_VECTORS as SHIPS
    rows = [row for row in bench.instances(None, args.set, args.language, 40) if row["repo"] not in GRADED]
    done = {json.loads(line)["instance"] for line in args.out.read_text().splitlines() if line.strip()} if args.out.exists() else set()
    ships = (args.language or "python") in SHIPS
    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60)
    with args.out.open("a") as out:
        for number, row in enumerate(rows, 1):
            if row["instance_id"] in done:
                continue
            entry = issue_row(row, args.where.resolve(), client, ships)
            if entry is None:
                print(f"{number:4}/{len(rows)} {row['instance_id']}: not prepared, left out", file=sys.stderr, flush=True)
                continue
            out.write(json.dumps(entry) + "\n")
            out.flush()
            if any(entry["fired"].values()):
                print(f"{number:4}/{len(rows)} {row['instance_id']:40} " + " ".join(f"{k} {v:.2f}" for k, v in entry["recall"].items()),
                      file=sys.stderr, flush=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["questions", "ceiling", "issues"])
    parser.add_argument("where", type=Path)
    parser.add_argument("out_or_none", type=Path, nargs="?")
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--set", default="locbench")
    parser.add_argument("--language")
    args = parser.parse_args(argv)
    if args.action == "issues":
        return issues(args)
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
        print(f"{number:4}/{len(rows)} {row['instance_id']:32} " + " ".join(f"{k} {v:.2f}" for k, v in entry["recall"].items())
              + f" beyond +{len(entry['beyond'])}", file=sys.stderr, flush=True)
    args.out.write_text(json.dumps(results, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
