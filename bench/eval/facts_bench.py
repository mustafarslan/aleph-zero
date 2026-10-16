"""The knowledge graph's facts on finding where a route is handled or an environment name is read (DEFERRED entry
0-graph, part 2; rules written before any run). Questions from `facts_questions.py`, prepared by Benchmark C's
`prepare` (`--set owner --questions FILE`). Four arms, each scored on its first ten distinct files:

- search:      search on the question, in the arm the repository's language ships with;
- search_name: search on the bare name (the path's last segment, or the environment name);
- facts:       `facts` (kind route or env, like the bare name), files in its order; its entries from documentation,
               `.env*` and OpenAPI files, and environment names only declared, are left out (DEFERRED 0-graph);
- facts_then_search: the files of `facts` first, then search's, ten in all (what an agent would use);
- handed:      what the plugin hands the agent (part 4): `facts(mentioned=question)`, its files in its order (routes,
               mounts, environment names, then the handlers' definitions), then search's, ten in all.

    facts_bench.py WORKDIR --questions FILE --out FILE
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402

from aleph_zero import archfacts  # noqa: E402
from aleph_zero.embed import worker  # noqa: E402
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402
from aleph_zero.vectors import VectorStore  # noqa: E402

SHIPS_WITH_VECTORS = {"javascript", "typescript", "rust", "c", "cpp"}
NEVER_AN_ANSWER = re.compile(r"(^|/)(docs?|documentation)/|\.(md|rst|txt|adoc)$|(^|/)\.env|openapi|swagger|"
                             r"(^|/)(docker-compose|compose)[^/]*\.ya?ml$|(^|/)Dockerfile", re.I)


def draw_of(instance: str) -> int:
    return 1 + int(hashlib.sha256(instance.encode()).hexdigest(), 16) % 2


def _ten(paths) -> list[str]:
    out: list[str] = []
    for path in paths:
        if path not in out:
            out.append(path)
        if len(out) == 10:
            break
    return out


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
                def nearest_for(text):
                    vector = client.embed([text], kind="query")[0]
                    return lambda limit, scoped: [c for c, _ in store.search(index, vector, limit, scoped)]
                nearest = nearest_for
        question, name = row["problem_statement"], row["like"]
        searched = _ten(hit.path for hit in bench.search(index, view, question, k=bench.DEEP,
                                                         dense=nearest(question) if nearest else None))
        by_name = _ten(hit.path for hit in bench.search(index, view, name, k=bench.DEEP, dense=nearest(name) if nearest else None))
        kind = "route" if row["category"] == "route" else "env"
        listed, _, _ = archfacts.select(archfacts.load(index), kind, name, 500)
        facts = _ten(found.path for found in listed if not NEVER_AN_ANSWER.search(found.path)
                     and not (kind == "env" and found.detail == "declared"))
        matched = archfacts.matching(archfacts.load(index), archfacts.mentioned(question))
        listed = matched + archfacts.handlers(index, view, [found for found in matched if found.kind == "route"])
        # As the engine hands them over: an OpenAPI document's own operations never (part 19).
        handed = list(dict.fromkeys(found.path for found in listed if not archfacts.is_spec_route(found)))
        arms = {"search": searched, "search_name": by_name, "facts": facts, "facts_then_search": _ten(facts + searched),
                "handed": _ten(handed + searched)}
        gold = set(row["gold"])

        def mrr(files):
            return next((1 / place for place, path in enumerate(files, 1) if path in gold), 0.0)
        return {"instance": row["instance_id"], "repo": row["repo"], "category": row["category"], "draw": draw_of(row["instance_id"]),
                "language": row["language"], "vectors": nearest is not None, "gold": sorted(gold), "files": arms,
                "recall": {arm: len(set(files) & gold) / len(gold) for arm, files in arms.items()},
                "mrr": {arm: mrr(files) for arm, files in arms.items()}}
    finally:
        index.conn.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = json.loads(args.questions.read_text())
    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60) \
        if any(row["language"] in SHIPS_WITH_VECTORS for row in rows) else None
    results = []
    for number, row in enumerate(rows, 1):
        entry = score_row(row, args.workdir.resolve(), client)
        if entry is None:
            print(f"{number:4}/{len(rows)} {row['instance_id']}: not prepared, left out", file=sys.stderr, flush=True)
            continue
        results.append(entry)
        print(f"{number:4}/{len(rows)} {row['instance_id']:32} " + " ".join(f"{arm} {value:.2f}" for arm, value in entry["recall"].items()),
              file=sys.stderr, flush=True)
    args.out.write_text(json.dumps(results, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
