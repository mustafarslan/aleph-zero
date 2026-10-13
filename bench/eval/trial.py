"""Changes to the ranking that cost nothing at query time, read on Benchmark C's kept indexes (docs/DEFERRED.md, 1d).

  uv run --group bench python bench/eval/trial.py score bench/data/benchc-go --set others --language go --only bench/data/results/benchmark_c_go.json --out bench/data/results/trial_go.json
  uv run --group bench python bench/eval/trial.py read bench/data/results/trial_*.json

A trial is a switch of the engine set for the run (`search.DOTTED_FALLBACK`, `search.PATH_VOTE`); `score` asks every
question with words and names and with vectors, as it is and with each trial and all of them together; nothing is
indexed or embedded but the questions. `read` gives each language (by its repositories' main language, as a
verdict is read) in the arm it ships with, and all questions together each in its language's shipped arm.
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

from aleph_zero import dense_policy, gitio  # noqa: E402
from aleph_zero import search as ranker  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_file, open_db  # noqa: E402

TRIALS = {"dotted": {"DOTTED_FALLBACK": True}, "path vote": {"PATH_VOTE": "found"}}
ARMS = {"as it is": {}, **TRIALS, "all": {name: value for switches in TRIALS.values() for name, value in switches.items()}}
FORMS = ("title", "full")
MEASURES = ("recall@10", "recall@1", "mrr")


@contextmanager
def switched(switches: dict):
    before = {name: getattr(ranker, name) for name in switches}
    for name, value in switches.items():
        setattr(ranker, name, value)
    try:
        yield
    finally:
        for name, value in before.items():
            setattr(ranker, name, value)


def score_all(workdir: Path, rows: list[dict], say) -> tuple[dict, dict]:
    from aleph_zero.embed import worker
    from aleph_zero.embed.client import VECTOR_KIND, EmbedClient
    from aleph_zero.vectors import VectorStore

    client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60)
    out: dict[str, dict] = {}
    left = {"left_out": 0}
    for number, row in enumerate(rows, 1):
        try:
            view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
        except gitio.GitError:
            left["left_out"] += 1
            continue
        index = Index(open_db(index_file(view.common_dir)))
        try:
            store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
            have, total = store.coverage(index)
            if index.sync(view, budget_seconds=0.0).pending or have < total:
                left["left_out"] += 1
                continue
            forms = bench.question_forms(index, row["problem_statement"])
            vectors = dict(zip(FORMS, client.embed([forms[form] for form in FORMS], kind="query")))
            entry: dict = {"repo": row["repo"], "arms": {}}
            for arm, switches in ARMS.items():
                with switched(switches):
                    entry["arms"][arm] = {}
                    for form in FORMS:
                        nearest = lambda limit, scoped, vector=vectors[form]: [  # noqa: E731
                            chunk_id for chunk_id, _ in store.search(index, vector, limit, scoped)]
                        words = bench.first_files([hit.path for hit in ranker.search(index, view, forms[form], k=10)])
                        both = bench.first_files([hit.path for hit in ranker.search(index, view, forms[form], k=10, dense=nearest)])
                        entry["arms"][arm][form] = {"lexical": bench.score(row["gold"], words), "hybrid": bench.score(row["gold"], both)}
            out[row["instance_id"]] = entry
        finally:
            index.conn.close()
        if number % 50 == 0:
            say(f"{number} of {len(rows)}")
    return out, left


def shipped_mode(language: str) -> str:
    verdict = dense_policy.VERDICTS.get(language)
    return "hybrid" if verdict is not None and verdict.helps else "lexical"


def compare(entries: list[tuple[dict, str]], arm: str, form: str = "title") -> dict:
    """`arm` against the engine as it is, over (question, the mode its language ships with) pairs."""
    out = {"questions": len(entries)}
    for measure in MEASURES:
        new = [entry["arms"][arm][form][mode][measure] for entry, mode in entries]
        old = [entry["arms"]["as it is"][form][mode][measure] for entry, mode in entries]
        gain, low, high = bench.paired_bootstrap(new, old)
        out[measure] = {"as it is": round(statistics.fmean(old), 4), "with it": round(statistics.fmean(new), 4), "gain": round(gain, 4),
                        "low": round(low, 4), "high": round(high, 4), "better": sum(a > b for a, b in zip(new, old)), "worse": sum(a < b for a, b in zip(new, old))}
    return out


def read(scored: dict[str, dict]) -> dict:
    """Per language in its shipped arm (and the other beside), and all questions together each in its own shipped arm."""
    by_language: dict[str, list[dict]] = {}
    for entry in scored.values():
        by_language.setdefault(entry["main_language"], []).append(entry)
    report: dict = {}
    for arm in ARMS:
        if arm == "as it is":
            continue
        block: dict = {}
        together = []
        for language, entries in sorted(by_language.items()):
            mode = shipped_mode(language)
            other = "lexical" if mode == "hybrid" else "hybrid"
            together += [(entry, mode) for entry in entries]
            block[language] = {"ships with": "vectors" if mode == "hybrid" else "words and names",
                               "first line": compare([(entry, mode) for entry in entries], arm),
                               "whole text": compare([(entry, mode) for entry in entries], arm, "full"),
                               "first line, the other arm": compare([(entry, other) for entry in entries], arm)}
        block["all, each in its shipped arm"] = {"first line": compare(together, arm), "whole text": compare(together, arm, "full")}
        report[arm] = block
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action", required=True)
    score = actions.add_parser("score")
    score.add_argument("workdir", type=Path)
    score.add_argument("--set", default="locbench")
    score.add_argument("--language")
    score.add_argument("--only", type=Path, required=True, help="a benchmark result: the questions it scored, with the main language it recorded")
    score.add_argument("--out", type=Path, required=True)
    reading = actions.add_parser("read")
    reading.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if args.action == "read":
        scored: dict[str, dict] = {}
        for path in args.files:
            for name, entry in json.loads(path.read_text())["scores"].items():
                if name in scored:
                    raise SystemExit(f"the same question in two files: {name}")
                scored[name] = entry
        print(json.dumps(read(scored), indent=1))
        return 0
    kept = {entry["instance"]: entry for entry in json.loads(args.only.read_text())["instances"]}
    rows = [row for row in bench.instances(None, args.set, args.language, None) if row["instance_id"] in kept]
    scored, left = score_all(args.workdir, rows, lambda text: print(text, file=sys.stderr, flush=True))
    for name, entry in scored.items():
        entry["main_language"] = kept[name].get("main_language") or ("python" if args.set == "locbench" else None)
        if not entry["main_language"]:
            raise SystemExit(f"no main language recorded for {name}: run population.py on {args.only} first")
    args.out.write_text(json.dumps({"left": left, "scores": scored}, indent=1) + "\n")
    print(f"{len(scored)} questions scored, {left['left_out']} left out", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
