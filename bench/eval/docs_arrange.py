"""Documentation answers and how search arranges code and documentation (DEFERRED entry 2, "Documentation answers":
the rule written before its questions existed).

Scores file recall@10 under the shipped arrangement and three candidates, in words-only and with-vectors arms:

- shipped:     code first, documentation capped at a quarter of the places, after all the code (`search.arrange`);
- earned:      (a) the places as the fused ranking gives them, no cap;
- half:        (b) documentation placed by its rank, at most half of the places;
- interleave:  (c) documentation placed by its rank, at most a quarter of the places (the shipped cap).

Questions come in the harness's shape (a prepared Benchmark C work directory: the owner's questions, Loc-Bench), or
asked of a live worktree whose linked sources are part of its view (`--worktree`: the vue/core case with its
documentation linked). Usage:

    docs_arrange.py WORKDIR --set owner --questions FILE [--category C] [--out FILE]
    docs_arrange.py WORKDIR --set locbench --form title [--limit N] [--out FILE]
    docs_arrange.py --worktree PATH --questions FILE [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402

from aleph_zero import search as search_module  # noqa: E402
from aleph_zero.embed import worker  # noqa: E402
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402
from aleph_zero.vectors import VectorStore  # noqa: E402
from aleph_zero.view import build_view  # noqa: E402

SHIPPED = search_module.arrange


def _placed(hits, k, kind, reserved):
    """Hits in their ranked order, documentation kept to `reserved` places; places code cannot fill go to the rest."""
    if kind != "any":
        return hits[:k]
    out, held, docs = [], [], 0
    for hit in hits:
        if hit.kind == "doc" and docs >= reserved:
            held.append(hit)
            continue
        docs += hit.kind == "doc"
        out.append(hit)
        if len(out) == k:
            return out
    return out + held[: k - len(out)]


ARRANGEMENTS = {
    "shipped": SHIPPED,
    "earned": lambda hits, k, kind: hits[:k],
    "half": lambda hits, k, kind: _placed(hits, k, kind, k // 2),
    "interleave": lambda hits, k, kind: _placed(hits, k, kind, min(max(2, k // 4), k // 2)),
}


def files_for(index, view, question, nearest, k=10) -> dict[str, dict[str, list[str]]]:
    """Each arrangement's first ten files, words only and with vectors."""
    found = {}
    for name, arrange in ARRANGEMENTS.items():
        search_module.arrange = arrange
        try:
            found[name] = {"lexical": bench.first_files([hit.path for hit in bench.search(index, view, question, k=k)]),
                           "hybrid": bench.first_files([hit.path for hit in bench.search(index, view, question, k=k, dense=nearest)])
                           if nearest else []}
        finally:
            search_module.arrange = SHIPPED
    return found


def _vectors(index, root, client, question):
    store = VectorStore(index.conn, root, VECTOR_KIND)
    have, total = store.coverage(index)
    if not total or have < total:
        return None
    vector = client.embed([question], kind="query")[0]
    return lambda limit, scoped: [chunk_id for chunk_id, _ in store.search(index, vector, limit, scoped)]


def score_rows(rows, view_of, client, form="full") -> list[dict]:
    results = []
    for number, row in enumerate(rows, 1):
        opened = view_of(row)
        if opened is None:
            continue
        index, view = opened
        question = bench.question_forms(index, row["problem_statement"])[form]
        nearest = _vectors(index, index_file(view.common_dir).parent, client, question) if client else None
        found = files_for(index, view, question, nearest)
        index.conn.close()
        results.append({"instance": row["instance_id"], "repo": row["repo"], "category": row.get("category"),
                        "draw": row.get("draw"), "gold": row["gold"], "files": found,
                        # An arm that ran and named no file scores 0; the with-vectors arm is left out only where it did not run.
                        "scores": {arr: {arm: bench.score(row["gold"], files)["recall@10"] for arm, files in arms.items()
                                         if arm == "lexical" or nearest}
                                   for arr, arms in found.items()}})
        print(f"{number:4}/{len(rows)} {row['instance_id']:32} " + " ".join(
            f"{arr}:{results[-1]['scores'][arr].get('lexical', 0):.2f}" for arr in ARRANGEMENTS), file=sys.stderr, flush=True)
    return results


def summary(results, arm: str) -> dict:
    """Mean recall@10 of each arrangement, and each candidate minus the shipped one with its paired interval."""
    rows = [entry for entry in results if arm in entry["scores"]["shipped"]]
    if not rows:
        return {}
    shipped = [entry["scores"]["shipped"][arm] for entry in rows]
    out = {"questions": len(rows), "shipped": round(sum(shipped) / len(rows), 3)}
    for name in ("earned", "half", "interleave"):
        values = [entry["scores"][name][arm] for entry in rows]
        mean, low, high = bench.paired_bootstrap(values, shipped)
        out[name] = {"recall@10": round(sum(values) / len(rows), 3), "minus_shipped": round(mean, 3), "low": round(low, 3), "high": round(high, 3)}
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path, nargs="?")
    parser.add_argument("--set", default="owner", choices=("owner", "locbench"))
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--category")
    parser.add_argument("--worktree", type=Path, help="ask a live worktree (its linked sources included)")
    parser.add_argument("--form", default="full", choices=bench.FORMS)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-vectors", action="store_true", help="words only: no worker needed")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    client = None if args.no_vectors else EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60)
    if args.worktree:
        rows = json.loads(args.questions.read_text())

        def view_of(row, root=args.worktree.resolve()):
            view = build_view(root)
            index = Index(open_db(index_file(view.common_dir)))
            index.sync(view, budget_seconds=120.0)
            return index, view
    else:
        rows = bench.instances(None, args.set, questions=args.questions)
        workdir = args.workdir.resolve()

        def view_of(row):
            try:
                view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
            except Exception:  # noqa: BLE001 - not fetched or prepared: left out, and counted by its absence
                return None
            index = Index(open_db(index_file(view.common_dir)))
            if index.sync(view, budget_seconds=0.0).pending:
                index.conn.close()
                return None
            return index, view
    if args.category:
        rows = [row for row in rows if row.get("category") == args.category]
    rows = rows[: args.limit]
    results = score_rows(rows, view_of, client, args.form)
    report = {"lexical": summary(results, "lexical"), "hybrid": summary(results, "hybrid")}
    for draw in sorted({entry["draw"] for entry in results if entry.get("draw")}):
        part = [entry for entry in results if entry.get("draw") == draw]
        report[f"draw{draw}"] = {"lexical": summary(part, "lexical"), "hybrid": summary(part, "hybrid")}
    print(json.dumps(report, indent=2))
    if args.out:
        args.out.write_text(json.dumps({"summary": report, "instances": results}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
