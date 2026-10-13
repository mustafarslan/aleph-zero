"""Changes to the ranking that cost nothing at query time, read on Benchmark C's kept indexes (docs/DEFERRED.md, 1d).

  uv run --group bench python bench/eval/trial.py score bench/data/benchc-go --set others --language go --only bench/data/results/benchmark_c_go.json --out bench/data/results/trial_go.json
  uv run --group bench python bench/eval/trial.py read bench/data/results/trial_*.json
  uv run --group bench python bench/eval/trial.py fetch bench/data/benchc-python-unasked --not-asked-in bench/data/results/benchmark_c_400.json
  uv run --group bench python bench/eval/trial.py score bench/data/benchc-python-unasked --words-only --not-asked-in ... --out ...

A trial is a switch of the engine set for the run (a module constant of `aleph_zero.search`, named in `TRIALS`);
`score` asks every question with words and names and with vectors, as it is and with each trial and all of them
together; nothing is indexed or embedded but the questions. The two trials of 2026-10-14 (a name written with what
it is called on looked up without it; a file's path voting for what was found in the file) were read twice and not
adopted (ADR-002); their code is in the history at commit 1a18dd4, and `TRIALS` is empty until the next. `read` gives each language (by its repositories' main language, as a
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

TRIALS: dict[str, dict] = {}        # a name -> the switches of `aleph_zero.search` it sets


def arms(trials: dict[str, dict]) -> dict[str, dict]:
    """The engine as it is, each trial, and (of two or more) all of them together."""
    together = {"all": {name: value for switches in trials.values() for name, value in switches.items()}} if len(trials) > 1 else {}
    return {"as it is": {}, **trials, **together}


ARMS = arms(TRIALS)
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


MOST_CHUNKS = 150_000       # of a commit: over it a question is left out, as in every language's benchmark


def fetch_all(workdir: Path, rows: list[dict], say) -> dict:
    """Each question's commit fetched (one snapshot) and indexed; nothing embedded. Network. What is there already
    is not fetched again."""
    counts = {"indexed": 0, "not fetched": 0, "over the limit": 0}
    for number, row in enumerate(rows, 1):
        try:
            clone = bench.fetch(workdir, row)
            view = bench.view_at(clone, row["base_commit"])
        except (OSError, gitio.GitError, bench.subprocess.CalledProcessError, bench.subprocess.TimeoutExpired) as wrong:
            counts["not fetched"] += 1
            say(f"{number:4}/{len(rows)} {row['instance_id']:44} not fetched: {str(wrong)[:70]}")
            continue
        index = Index(open_db(index_file(view.common_dir)))
        try:
            while index.sync(view, budget_seconds=600).pending:
                pass
            chunks = index.counts()["chunks_in_view"]
        finally:
            index.conn.close()
        counts["over the limit" if chunks > MOST_CHUNKS else "indexed"] += 1
        say(f"{number:4}/{len(rows)} {row['instance_id']:44} files {len(view.files):6} chunks {chunks:7}")
    return counts


def score_all(workdir: Path, rows: list[dict], say, words_only: bool = False) -> tuple[dict, dict]:
    """Every question in every arm. With `words_only` nothing is asked of the worker: for a language that ships
    with words and names, on questions whose repositories were never embedded."""
    out: dict[str, dict] = {}
    left = {"left_out": 0}
    client = store = None
    if not words_only:
        from aleph_zero.embed import worker
        from aleph_zero.embed.client import VECTOR_KIND, EmbedClient
        from aleph_zero.vectors import VectorStore

        client = EmbedClient(worker.ensure(), capacity=worker.capacity_tokens(), timeout=60)
    for number, row in enumerate(rows, 1):
        try:
            view = bench.view_at(bench.clone_of(workdir, row["repo"]), row["base_commit"])
        except (OSError, gitio.GitError):
            left["left_out"] += 1
            continue
        index = Index(open_db(index_file(view.common_dir)))
        try:
            if index.sync(view, budget_seconds=0.0).pending or index.counts()["chunks_in_view"] > MOST_CHUNKS:
                left["left_out"] += 1
                continue
            if not words_only:
                store = VectorStore(index.conn, index_file(view.common_dir).parent, VECTOR_KIND)
                have, total = store.coverage(index)
                if have < total:
                    left["left_out"] += 1
                    continue
            forms = bench.question_forms(index, row["problem_statement"])
            vectors = None if words_only else dict(zip(FORMS, client.embed([forms[form] for form in FORMS], kind="query")))
            entry: dict = {"repo": row["repo"], "arms": {}}
            for arm, switches in ARMS.items():
                with switched(switches):
                    entry["arms"][arm] = {}
                    for form in FORMS:
                        words = bench.first_files([hit.path for hit in ranker.search(index, view, forms[form], k=10)])
                        scores = {"lexical": bench.score(row["gold"], words)}
                        if vectors is not None:
                            nearest = lambda limit, scoped, vector=vectors[form]: [  # noqa: E731
                                chunk_id for chunk_id, _ in store.search(index, vector, limit, scoped)]
                            both = bench.first_files([hit.path for hit in ranker.search(index, view, forms[form], k=10, dense=nearest)])
                            scores["hybrid"] = bench.score(row["gold"], both)
                        entry["arms"][arm][form] = scores
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
    found = next(iter(scored.values()))["arms"] if scored else {}
    for arm in found:
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
                               "whole text": compare([(entry, mode) for entry in entries], arm, "full")}
            if all(other in entry["arms"][arm]["title"] for entry in entries):      # a words-only reading has no other arm
                block[language]["first line, the other arm"] = compare([(entry, other) for entry in entries], arm)
        block["all, each in its shipped arm"] = {"first line": compare(together, arm), "whole text": compare(together, arm, "full")}
        report[arm] = block
    return report


def given(text: str) -> tuple[str, dict]:
    """A trial written on the command line: `NAME:CONSTANT=VALUE`. The constant must be one the ranking has."""
    name, _, setting = text.partition(":")
    constant, _, value = setting.partition("=")
    if not (name and constant and value) or not hasattr(ranker, constant):
        raise SystemExit(f"not a trial: {text!r} (NAME:CONSTANT=VALUE, a constant of aleph_zero.search)")
    try:
        return name, {constant: json.loads(value)}
    except ValueError:
        return name, {constant: value}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    actions = parser.add_subparsers(dest="action", required=True)
    for name in ("score", "fetch"):
        action = actions.add_parser(name)
        action.add_argument("workdir", type=Path)
        action.add_argument("--set", default="locbench")
        action.add_argument("--language")
        action.add_argument("--only", type=Path, help="a benchmark result: the questions it scored, with the main language it recorded")
        action.add_argument("--not-asked-in", type=Path, nargs="+", help="benchmark results: the questions none of them scored")
        action.add_argument("--repos", help="comma-separated owner/name: only these repositories' questions")
        if name == "score":
            action.add_argument("--trial", action="append", default=[], metavar="NAME:CONSTANT=VALUE",
                                help="a trial for this run: a constant of aleph_zero.search set to a value (RRF20:RRF_K=20); may be given again")
            action.add_argument("--words-only", action="store_true", help="ask nothing of the worker: for a language that ships with words and names")
            action.add_argument("--out", type=Path, required=True)
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
    if bool(args.only) == bool(args.not_asked_in):
        parser.error("one of --only and --not-asked-in")
    if getattr(args, "trial", None):
        global ARMS
        ARMS = arms({**TRIALS, **dict(given(text) for text in args.trial)})
    say = lambda text: print(text, file=sys.stderr, flush=True)  # noqa: E731
    repos = set(args.repos.split(",")) if args.repos else None
    every = bench.instances(repos, args.set, args.language, None)
    if args.only:
        kept = {entry["instance"]: entry for entry in json.loads(args.only.read_text())["instances"]}
        rows = [row for row in every if row["instance_id"] in kept]
    else:
        kept = {}
        rows = [row for row in every if row["instance_id"] not in bench.asked_in(args.not_asked_in)]
    if args.action == "fetch":
        print(json.dumps(fetch_all(args.workdir, rows, say)))
        return 0
    scored, left = score_all(args.workdir, rows, say, args.words_only)
    for name, entry in scored.items():
        # Never-asked questions of a set of one language (Loc-Bench: Python; `--language`) are that language's: their
        # repositories were not embedded, so nothing but the set says what they are.
        entry["main_language"] = kept.get(name, {}).get("main_language") or ("python" if args.set == "locbench" else args.language if not args.only else None)
        if not entry["main_language"]:
            raise SystemExit(f"no main language recorded for {name}: run population.py on {args.only} first")
    args.out.write_text(json.dumps({"left": left, "scores": scored}, indent=1) + "\n")
    print(f"{len(scored)} questions scored, {left['left_out']} left out", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
