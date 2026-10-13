"""Fusing by definition instead of by piece, read from the benchmark's side (docs/DEFERRED.md, entry 1c).

  uv run --group bench python bench/eval/fusion.py bench/data/benchc --only bench/data/results/benchmark_c_400.json --out ...
  uv run --group bench python bench/eval/fusion.py bench/data/benchc-cpp --set others --language cpp --per-repo 40 --out ...

The engine's fusion adds up the generators' votes per chunk, and the pieces of one long definition are merged into
one result only afterwards. Here the fusion is replaced for the run, on the prepared indexes, in two arms beside the
fusion as it is: `merged` (the pieces of a definition are one candidate in each generator, at the rank of its best
piece, and the generators' votes for it are added) and `closed` (the same, and each list is numbered by definitions).
No engine code is changed and nothing is indexed or embedded; the question's vector is asked of the worker.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402

from aleph_zero import gitio  # noqa: E402
from aleph_zero import search as ranker  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402

ARMS = {"as it is": None, "merged": False, "closed": True}      # name -> close_up, None for the engine's own fusion
FORMS = ("title", "full")
MEASURES = ("recall@10", "recall@1", "mrr")
_TIE = {"symbol": 0, "lexical": 1}      # on a tie the name generator's piece carries the definition, then the words'


def definition_keys(index: Index, chunk_ids: list[int]) -> tuple[dict[int, tuple], int]:
    """For each chunk its candidate: the definition it is a piece of, or itself. Exactly what `search.merge_pieces`
    merges after fusing: the same file content, the same name and the same recorded scope, and only with a name and
    a recorded scope (a definition that was not split and a module's top level record none). A chunk that two files
    of the view share would belong to two definitions: it is its own candidate, and how many there were is returned."""
    keys: dict[int, tuple] = {chunk_id: ("chunk", chunk_id) for chunk_id in chunk_ids}
    found: dict[int, set[tuple]] = {}
    wanted = list(dict.fromkeys(chunk_ids))
    for start in range(0, len(wanted), 500):
        some = wanted[start:start + 500]
        for chunk_id, oid, family, symbol, scope_start, scope_end in index.conn.execute(
                "SELECT bc.chunk_id, bc.blob_oid, bc.family, c.symbol, bc.scope_start, bc.scope_end FROM blob_chunks bc "
                "JOIN chunks c ON c.chunk_id = bc.chunk_id JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid AND v.family = bc.family "
                f"WHERE bc.chunk_id IN ({','.join('?' * len(some))})", some):
            if symbol and scope_start:
                found.setdefault(chunk_id, set()).add(("def", oid, family, symbol, scope_start, scope_end))
    shared = 0
    for chunk_id, definitions in found.items():
        if len(definitions) == 1:
            keys[chunk_id] = next(iter(definitions))
        else:
            shared += 1
    return keys, shared


def by_definition(rankings: dict[str, list[int]], keys: dict[int, tuple], close_up: bool) -> list[tuple[int, float, tuple[str, ...]]]:
    """Reciprocal rank fusion over definitions, in the shape `search.rrf` returns: (chunk, score, generators), best
    first. A definition's rank in a generator is that of its best piece: among chunks as they were, or with
    `close_up` among definitions, so that what stood behind its second piece moves up. Its score goes to one
    piece, the one with the best rank in any generator; its other pieces are not candidates any more."""
    scores: dict[tuple, float] = {}
    sources: dict[tuple, list[str]] = {}
    carrier: dict[tuple, tuple[int, int, int]] = {}             # definition -> (rank, tie order, chunk) of its best-placed piece
    for name, ranked in rankings.items():
        weight = ranker.GENERATOR_WEIGHT.get(name, 1.0)
        seen: set[tuple] = set()
        for position, chunk_id in enumerate(dict.fromkeys(ranked), start=1):
            key = keys.get(chunk_id, ("chunk", chunk_id))
            if key in seen:
                continue                                        # a later piece of a definition this generator already voted for
            seen.add(key)
            rank = len(seen) if close_up else position
            scores[key] = scores.get(key, 0.0) + weight / (ranker.RRF_K + rank)
            sources.setdefault(key, []).append(name)
            placed = (rank, _TIE.get(name, 2), chunk_id)
            if key not in carrier or placed < carrier[key]:
                carrier[key] = placed
    return sorted(((carrier[key][2], score, tuple(sources[key])) for key, score in scores.items()),
                  key=lambda item: (-item[1], item[0]))


@contextmanager
def fused(index: Index, close_up: bool):
    """The engine's fusion replaced by fusion over definitions, for searches of `index` inside the block."""
    before = ranker.rrf
    seen = {"searches": 0, "shared": 0}

    def replaced(rankings: dict[str, list[int]]):
        keys, shared = definition_keys(index, [chunk_id for ranked in rankings.values() for chunk_id in ranked])
        seen["searches"] += 1
        seen["shared"] += shared
        return by_definition(rankings, keys, close_up)

    ranker.rrf = replaced
    try:
        yield seen
    finally:
        ranker.rrf = before


def score_all(workdir: Path, rows: list[dict], say) -> tuple[dict[str, dict[str, dict]], dict]:
    """Every question in every arm, with words and names and with vectors, from the prepared indexes."""
    from aleph_zero.embed import worker
    from aleph_zero.embed.client import VECTOR_KIND, EmbedClient
    from aleph_zero.vectors import VectorStore

    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60)
    out: dict[str, dict[str, dict]] = {arm: {} for arm in ARMS}
    notes = {"left_out": 0, "searches": 0, "chunks_shared_by_two_definitions": 0}
    for number, row in enumerate(rows, 1):
        try:
            view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
        except gitio.GitError:
            notes["left_out"] += 1
            continue
        index = Index(open_db(index_file(view.common_dir)))
        try:
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            if index.sync(view, budget_seconds=0.0).pending or store.coverage(index)[0] < store.coverage(index)[1]:
                notes["left_out"] += 1
                continue
            forms = bench.question_forms(index, row["problem_statement"])
            vectors = dict(zip(FORMS, client.embed([forms[form] for form in FORMS], kind="query")))

            def ask(form: str) -> dict:
                nearest = lambda limit, scoped, vector=vectors[form]: [  # noqa: E731
                    chunk_id for chunk_id, _ in store.search(index, vector, limit, scoped)]
                words = bench.first_files([hit.path for hit in ranker.search(index, view, forms[form], k=10)])
                both = bench.first_files([hit.path for hit in ranker.search(index, view, forms[form], k=10, dense=nearest)])
                return {"lexical": bench.score(row["gold"], words), "hybrid": bench.score(row["gold"], both)}

            for arm, close_up in ARMS.items():
                if close_up is None:
                    out[arm][row["instance_id"]] = {form: ask(form) for form in FORMS}
                else:
                    with fused(index, close_up) as seen:
                        out[arm][row["instance_id"]] = {form: ask(form) for form in FORMS}
                    notes["searches"] += seen["searches"]
                    notes["chunks_shared_by_two_definitions"] += seen["shared"]
        finally:
            index.conn.close()
        if number % 50 == 0:
            say(f"{number} of {len(rows)}")
    return out, notes


def summarise(scored: dict[str, dict[str, dict]]) -> dict:
    """Each arm's means, and each of the two new arms against the fusion as it is (and `closed` against `merged`)."""
    names = sorted(set.intersection(*(set(entry) for entry in scored.values())))
    out: dict = {"questions": len(names)}
    series = lambda arm, form, mode, measure: [scored[arm][name][form][mode][measure] for name in names]  # noqa: E731
    for form in FORMS:
        for mode in ("lexical", "hybrid"):
            block: dict = {arm: {measure: round(statistics.fmean(series(arm, form, mode, measure)), 3) for measure in MEASURES} for arm in scored}
            for arm, base in (("merged", "as it is"), ("closed", "as it is"), ("closed", "merged")):
                block[f"{arm} against {base}"] = {}
                for measure in MEASURES:
                    mean, low, high = bench.paired_bootstrap(series(arm, form, mode, measure), series(base, form, mode, measure))
                    block[f"{arm} against {base}"][measure] = {"difference": round(mean, 3), "low": round(low, 3), "high": round(high, 3)}
            out[f"{form}, {'words and names' if mode == 'lexical' else 'with vectors'}"] = block
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--set", default="locbench")
    parser.add_argument("--language")
    parser.add_argument("--per-repo", type=int)
    parser.add_argument("--only", type=Path, help="a benchmark result file: only the questions it scored")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    rows = bench.instances(None, args.set, args.language, args.per_repo)
    if args.only:
        wanted = {entry["instance"] for entry in json.loads(args.only.read_text())["instances"]}
        rows = [row for row in rows if row["instance_id"] in wanted]
    scored, notes = score_all(args.workdir, rows, lambda text: print(text, file=sys.stderr, flush=True))
    report = {**summarise(scored), **notes}
    print(json.dumps(report, indent=2))
    if args.out:
        args.out.write_text(json.dumps({"summary": report, "scores": scored}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
