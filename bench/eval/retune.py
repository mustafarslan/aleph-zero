"""The ranking's starting values, each tried alone against what ships (docs/DEFERRED.md, entry 1c (b)).

  uv run --group bench python bench/eval/retune.py table bench/data/benchc --only FIRST.json --out TABLE.json
  uv run --group bench python bench/eval/retune.py read  bench/data/benchc --only ALL.json --without FIRST.json --candidate '{"path_candidates": 8}'
  uv run --group bench python bench/eval/retune.py read  bench/data/benchc-cpp --set others --language cpp --per-repo 40 --dense --candidate ...

Every value here acts when a question is asked: nothing is indexed or embedded again, and the prepared indexes are
only read. `table` tries each value of each list alone, with words and names, and prints what goes forward by the
rule written before the first run. `read` scores one candidate against what ships on the questions it was not
chosen on, with paired intervals.
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

from aleph_zero import gitio, lexical  # noqa: E402
from aleph_zero import search as ranker  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402

SHIPPED = {"weights": lexical.WEIGHTS, "long_query": (lexical.LONG_QUERY, lexical.KEEP_TERMS),
           "path_candidates": ranker.PATH_CANDIDATES, "symbol_candidates": ranker.SYMBOL_CANDIDATES, "symbol_weight": 1.0}
VALUES: dict[str, list] = {      # written before the first run; what ships is in every list
    "weights": [(5.0, 3.0, 2.0, 1.0), (3.0, 2.0, 1.5, 1.0), (8.0, 4.0, 2.0, 1.0), (5.0, 3.0, 1.0, 1.0), (2.0, 1.0, 1.0, 1.0), (1.0, 1.0, 1.0, 1.0)],
    # (the length at which a query starts to lose words, how many it keeps): never fewer kept than the length it is cut at
    "long_query": [(16, 24), (12, 16), (12, 24), (16, 32), (16, 48), (24, 32), (24, 48)],
    "path_candidates": [3, 5, 8],
    "symbol_candidates": [5, 10, 20],
    "symbol_weight": [1.0, 1.5, 2.0],
}
WHOLE_TEXT_ONLY = {"long_query"}   # a first line is hardly ever long enough to be cut
FORWARD_GAIN = 0.015               # 1.5 points on the questions the table is made on
FORWARD_MOST = 2
FORMS = ("title", "full")


@contextmanager
def ranking(**changed):
    """The ranking's values as given inside the block; what ships outside it and for whatever is not named."""
    unknown = set(changed) - set(SHIPPED)
    if unknown:
        raise ValueError(f"not a value of the ranking: {', '.join(sorted(unknown))}")
    values = {**SHIPPED, **changed}
    before = (lexical.WEIGHTS, lexical.LONG_QUERY, lexical.KEEP_TERMS, ranker.PATH_CANDIDATES, ranker.SYMBOL_CANDIDATES,
              dict(ranker.GENERATOR_WEIGHT))
    lexical.WEIGHTS = tuple(float(weight) for weight in values["weights"])
    lexical.LONG_QUERY, lexical.KEEP_TERMS = values["long_query"]
    ranker.PATH_CANDIDATES, ranker.SYMBOL_CANDIDATES = values["path_candidates"], values["symbol_candidates"]
    ranker.GENERATOR_WEIGHT.clear()
    if values["symbol_weight"] != 1.0:
        ranker.GENERATOR_WEIGHT["symbol"] = float(values["symbol_weight"])
    try:
        yield
    finally:
        (lexical.WEIGHTS, lexical.LONG_QUERY, lexical.KEEP_TERMS, ranker.PATH_CANDIDATES, ranker.SYMBOL_CANDIDATES) = before[:5]
        ranker.GENERATOR_WEIGHT.clear()
        ranker.GENERATOR_WEIGHT.update(before[5])


def label(name: str, value) -> str:
    return f"{name}={json.dumps(value)}"


def _unlabel(text: str) -> tuple[str, object]:
    name, _, value = text.partition("=")
    parsed = json.loads(value)
    return name, tuple(parsed) if isinstance(parsed, list) else parsed


def measure(scores: dict, name: str) -> float:
    """What a question counts for: file recall@10, the mean of both forms; for long-query values whole text alone."""
    if name in WHOLE_TEXT_ONLY:
        return scores["full"]["recall@10"]
    return (scores["title"]["recall@10"] + scores["full"]["recall@10"]) / 2


def table(by_variant: dict[str, dict[str, dict]], names: list[str]) -> dict[str, dict]:
    """For each variant: its measure over the questions, and the gain over what ships for the same value."""
    out: dict[str, dict] = {}
    for text, scored in by_variant.items():
        name, _ = _unlabel(text)
        shipped = by_variant[label(name, SHIPPED[name])]
        mine = statistics.fmean(measure(scored[question], name) for question in names)
        base = statistics.fmean(measure(shipped[question], name) for question in names)
        out[text] = {"measure": round(mine, 4), "gain": round(mine - base, 4)}
    return out


def forward(made: dict[str, dict]) -> dict:
    """The changes that go on: the best of each value if it gains FORWARD_GAIN, the FORWARD_MOST largest of those."""
    best: dict[str, tuple[float, object]] = {}
    for text, entry in made.items():
        name, value = _unlabel(text)
        if entry["gain"] >= FORWARD_GAIN and entry["gain"] > best.get(name, (0.0, None))[0]:
            best[name] = (entry["gain"], value)
    chosen = sorted(best.items(), key=lambda item: -item[1][0])[:FORWARD_MOST]
    return {name: value for name, (_, value) in chosen}


def _questions(args) -> list[dict]:
    rows = bench.instances(None, args.set, args.language, args.per_repo)
    ids = lambda path: {entry["instance"] for entry in json.loads(path.read_text())["instances"]}  # noqa: E731
    if args.only:
        rows = [row for row in rows if row["instance_id"] in ids(args.only)]
    if args.without:
        rows = [row for row in rows if row["instance_id"] not in ids(args.without)]
    return rows


def score_all(workdir: Path, rows: list[dict], variants: dict[str, dict], dense: bool, say) -> dict[str, dict[str, dict]]:
    """Every question under every variant, from the prepared indexes (read, never chunked or embedded here)."""
    client = None
    if dense:
        from aleph_zero.embed import worker
        from aleph_zero.embed.client import VECTOR_KIND, EmbedClient
        from aleph_zero.vectors import VectorStore

        client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60)
    out: dict[str, dict[str, dict]] = {text: {} for text in variants}
    left = 0
    for number, row in enumerate(rows, 1):
        try:
            view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
        except gitio.GitError:
            left += 1
            continue
        index = Index(open_db(index_file(view.common_dir)))
        try:
            if index.sync(view, budget_seconds=0.0).pending:
                left += 1
                continue
            forms = bench.question_forms(index, row["problem_statement"])
            nearest = {}
            if client is not None:
                store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
                have, total = store.coverage(index)
                if have < total:
                    left += 1
                    continue
                for form, vector in zip(FORMS, client.embed([forms[form] for form in FORMS], kind="query")):
                    nearest[form] = lambda limit, scoped, vector=vector, store=store, index=index: [  # noqa: E731
                        chunk_id for chunk_id, _ in store.search(index, vector, limit, scoped)]
            for text, changed in variants.items():
                with ranking(**changed):
                    entry: dict = {}
                    for form in FORMS:
                        files = bench.first_files([hit.path for hit in ranker.search(index, view, forms[form], k=10)])
                        entry[form] = bench.score(row["gold"], files)
                        if client is not None:
                            files = bench.first_files([hit.path for hit in ranker.search(index, view, forms[form], k=10, dense=nearest[form])])
                            entry[form]["hybrid"] = bench.score(row["gold"], files)
                    out[text][row["instance_id"]] = entry
        finally:
            index.conn.close()
        if number % 50 == 0:
            say(f"{number} of {len(rows)}")
    if left:
        say(f"left out, not prepared: {left}")
    return out


def compare(shipped: dict[str, dict], candidate: dict[str, dict], arm: str | None = None) -> dict:
    """The candidate against what ships on the same questions: both forms, their mean, with paired intervals."""
    names = sorted(set(shipped) & set(candidate))
    pick = lambda scores, form: (scores[form][arm] if arm else scores[form])["recall@10"]  # noqa: E731
    out: dict = {"questions": len(names)}
    series = {"first line": lambda s: pick(s, "title"), "whole text": lambda s: pick(s, "full"),
              "both": lambda s: (pick(s, "title") + pick(s, "full")) / 2}
    for what, of in series.items():
        mine, base = [of(candidate[name]) for name in names], [of(shipped[name]) for name in names]
        mean, low, high = bench.paired_bootstrap(mine, base)
        out[what] = {"shipped": round(statistics.fmean(base), 3), "candidate": round(statistics.fmean(mine), 3),
                     "difference": round(mean, 3), "low": round(low, 3), "high": round(high, 3)}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("table", "read"))
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--set", default="locbench")
    parser.add_argument("--language")
    parser.add_argument("--per-repo", type=int)
    parser.add_argument("--only", type=Path, help="a benchmark result file: only the questions it scored")
    parser.add_argument("--without", type=Path, help="a benchmark result file: not the questions it scored")
    parser.add_argument("--candidate", help="for `read`: the changed values as JSON, e.g. '{\"path_candidates\": 8}'")
    parser.add_argument("--dense", action="store_true", help="for `read`: the arm with vectors beside words and names (needs the worker)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    say = lambda text: print(text, file=sys.stderr, flush=True)  # noqa: E731
    rows = _questions(args)
    if args.command == "table":
        variants = {label(name, value): {name: value} for name, values in VALUES.items() for value in values}
        scored = score_all(args.workdir, rows, variants, False, say)
        names = sorted(set.intersection(*(set(entry) for entry in scored.values())))
        made = table(scored, names)
        report = {"questions": len(names), "table": made, "forward": forward(made)}
    else:
        changed = {name: tuple(value) if isinstance(value, list) else value for name, value in json.loads(args.candidate or "{}").items()}
        scored = score_all(args.workdir, rows, {"shipped": {}, "candidate": changed}, args.dense, say)
        report = {"candidate": changed, "words and names": compare(scored["shipped"], scored["candidate"])}
        if args.dense:
            report["with vectors"] = compare(scored["shipped"], scored["candidate"], arm="hybrid")
    print(json.dumps(report, indent=2, default=list))
    if args.out:
        args.out.write_text(json.dumps({**report, "scores": scored}, indent=1, default=list) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
