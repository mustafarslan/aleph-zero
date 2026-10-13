"""A language's benchmark result read by the gate of ADR-002, the same way for every language.

  uv run --group bench python bench/eval/verdict.py bench/data/results/benchmark_c_go.json
  uv run --group bench python bench/eval/verdict.py --main-language javascript benchmark_c_javascript.json benchmark_c_typescript.json

A verdict is applied by a repository's main language, so with `--main-language` the questions are those whose
repository the engine calls that language at the question's commit, from every file given, whatever set or label
they came under (`population.py` writes that language into a result file; docs/DEFERRED.md, entry 1b-2).

The gate: on the question's first line, vectors add 5 points or more of file recall@10 with a paired
interval that excludes zero. Beside it and deciding nothing: the whole issue text, the interval when
repositories are resampled instead of questions, and the same reading on the repositories whose commits
stay at or under 50,000 chunks (the owner asked for mid-sized repositories; docs/DEFERRED.md, entry 1b).
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_c as bench  # noqa: E402

BAR = 0.05
REPOSITORIES = 4            # fewer after the size limit: no verdict
MID = 50_000                # chunks of a commit: the reading beside the verdict
FORMS = {"first line": "title", "whole text": "full"}


def _by_repository(rows: list[dict], form: str, rounds: int = 10_000, seed: int = 20261012) -> list[float]:
    """The interval of the gain when repositories are drawn again instead of questions."""
    groups: dict[str, list[float]] = {}
    for row in rows:
        groups.setdefault(row["repo"], []).append(row["scores"][form]["hybrid"]["recall@10"] - row["scores"][form]["lexical"]["recall@10"])
    names, rng, means = sorted(groups), random.Random(seed), []
    for _ in range(rounds):
        drawn = [value for name in (rng.choice(names) for _ in names) for value in groups[name]]
        means.append(statistics.fmean(drawn))
    means.sort()
    return [round(means[int(0.025 * rounds)], 3), round(means[int(0.975 * rounds)], 3)]


def _form(rows: list[dict], form: str) -> dict:
    words = [row["scores"][form]["lexical"]["recall@10"] for row in rows]
    vectors = [row["scores"][form]["hybrid"]["recall@10"] for row in rows]
    gain, low, high = bench.paired_bootstrap(vectors, words)
    return {"words": round(statistics.fmean(words), 3), "vectors": round(statistics.fmean(vectors), 3),
            "gain": gain, "low": low, "high": high,
            "better": sum(a > b for a, b in zip(vectors, words)), "worse": sum(a < b for a, b in zip(vectors, words)),
            "first place": [round(statistics.fmean(row["scores"][form][mode]["recall@1"] for row in rows), 3) for mode in ("lexical", "hybrid")],
            "by repository": _by_repository(rows, form)}


def read(rows: list[dict]) -> dict:
    """The verdict of a result's questions: `passes` is True, False, or None where there is none to give."""
    repositories = sorted({row["repo"] for row in rows})
    out: dict = {"questions": len(rows), "repositories": len(repositories),
                 "questions a repository": {name: sum(row["repo"] == name for row in rows) for name in repositories}}
    for label, form in FORMS.items():
        out[label] = _form(rows, form)
    first = out["first line"]
    if len(repositories) < REPOSITORIES:
        out["passes"], out["why"] = None, f"{len(repositories)} repositories: a language is read with four or more"
    else:
        out["passes"] = bool(first["gain"] >= BAR and first["low"] > 0)
        out["why"] = ("5 points or more, and the interval excludes zero" if out["passes"]
                      else "the interval holds zero" if first["low"] <= 0 else "the gain is real and under 5 points")
    largest: dict[str, int] = {}
    for row in rows:
        largest[row["repo"]] = max(largest.get(row["repo"], 0), row["chunks"])
    small = [row for row in rows if largest[row["repo"]] <= MID]
    left = sorted(name for name, size in largest.items() if size > MID)
    beside: dict = {"questions": len(small), "left out": left}
    if small and left:
        beside["first line"], beside["whole text"] = _form(small, "title"), _form(small, "full")
    elif small:
        beside["first line"] = first      # nothing is over it: the same questions
    out[f"repositories at or under {MID:,} chunks"] = beside
    return out


def of_main_language(rows: list[dict], language: str) -> list[dict]:
    """The questions whose repository's main language is `language`. A question whose repository's language was
    never read, or one that is there twice, ends the reading: neither is quietly counted or dropped."""
    unread = [row["instance"] for row in rows if not row.get("main_language")]
    if unread:
        raise SystemExit(f"no main language recorded for {len(unread)} questions ({', '.join(unread[:3])}): run population.py on their file first")
    names = [row["instance"] for row in rows]
    twice = sorted({name for name in names if names.count(name) > 1})
    if twice:
        raise SystemExit(f"the same question in two files: {', '.join(twice[:3])}")
    return [row for row in rows if row["main_language"] == language]


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", type=Path, nargs="+")
    parser.add_argument("--main-language", help="only the questions whose repository's main language is this, from all the files")
    args = parser.parse_args(argv)
    found = [json.loads(path.read_text()) for path in args.results]
    if len(found) > 1 and not args.main_language:
        parser.error("several files are read together only by --main-language")
    rows = [row for one in found for row in one["instances"]]
    report = read(of_main_language(rows, args.main_language) if args.main_language else rows)
    report["left out of the run"] = found[0]["summary"].get("left_out", {}) if len(found) == 1 else [one["summary"].get("left_out", {}) for one in found]
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
