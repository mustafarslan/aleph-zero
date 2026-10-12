"""Does Aleph's search find the decision that governs a change? Graded on oxn's labelled pairs.

Usage:
  uv run python bench/eval/decisions.py bench/data/decisions [--dense] [--out FILE]

The protocol is fixed in the M8 plan and taken from oxn (`scripts/extract_citation_labels.py`,
`scripts/measure_semantic.py`, v0.1.2, MIT, same author), because the bar is oxn's figure:

- pairs: a commit that cites an ADR while doing the work it governs; gold = the cited ADR(s);
- task text: the commit's subject and body minus the citation, the conventional-commit prefix,
  issue numbers, trailers and URLs;
- candidates: the numbered Markdown files directly in the ADR directory at the pinned commit,
  in a scratch repository of their own; Aleph's ordinary search ranks them, a document by its
  best chunk;
- measures: precision@1 and @3 (any gold ADR among the first one or three documents), MRR.

oxn's BM25 on the same 100 pairs: 0.610 / 0.820 / 0.726. The bar is precision@3 above 0.820.
`<workdir>` holds a clone of each corpus and `labels-external.json`. Nothing is tuned here.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from aleph_zero.chunking import estimate_tokens
from aleph_zero.engine import Engine

OXN = {"p_at_1": 0.610, "p_at_3": 0.820, "mrr": 0.726}
# oxn's two expressions: a citation, and what around it is not the task.
CITATION = re.compile(r"\bADR[- _]?(\d{1,4})\b", re.I)
NOISE = re.compile(
    r"^\w+(\([^)]*\))?!?:|\(#\d+\)|#\d+|\b[A-Z]{2,}-\d+\b"
    r"|^\s*(Co-Authored-By|Closes|Fixes|Refs|Signed-off-by):.*$|https?://\S+",
    re.M | re.I,
)
_NUMBERED = re.compile(r"\d+[-_]")
K = 60      # chunks asked for: enough for every decision of a corpus to appear


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def task_text(root: Path, sha: str) -> str:
    """What a retriever would be given: the commit, minus the answer and the plumbing."""
    subject, _, body = git(root, "log", "-1", "--format=%s\x1f%b", sha).partition("\x1f")
    return NOISE.sub(" ", CITATION.sub(" ", f"{subject.strip()}\n{body}")).strip()


def decisions(root: Path, sha: str, adr_dir: str) -> dict[str, int]:
    """path -> the number a citation would name, for each numbered decision at the pinned commit."""
    found = {}
    for path in git(root, "ls-tree", "--name-only", sha, adr_dir.rstrip("/") + "/").split("\n"):
        stem = Path(path).stem
        if path.endswith(".md") and _NUMBERED.match(stem):
            found[path] = int(re.sub(r"\D", "", stem) or -1)      # as oxn numbers it
    return found


def scratch_repository(root: Path, sha: str, files: dict[str, int], where: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(where)], check=True)
    for path in files:
        (where / path).parent.mkdir(parents=True, exist_ok=True)
        (where / path).write_bytes(subprocess.run(["git", "-C", str(root), "show", f"{sha}:{path}"], capture_output=True, check=True).stdout)
    return where


def ranked(engine: Engine, query: str, numbers: dict[str, int]) -> tuple[list[int], int]:
    """The decisions in the order search returns them, each at its best chunk; and the tokens of the answer as rendered."""
    answer = engine.search(query, k=K)
    order: list[int] = []
    for hit in answer.hits:
        number = numbers.get(hit.path)
        if number is not None and number not in order:
            order.append(number)
    return order, estimate_tokens(engine.render(answer)) if answer.hits else 0


def score(rows: list[dict]) -> dict:
    if not rows:
        return {"pairs": 0}
    at = lambda k: sum(any(number in row["gold"] for number in row["order"][:k]) for row in rows) / len(rows)  # noqa: E731
    reciprocal = sum(next((1 / (rank + 1) for rank, number in enumerate(row["order"]) if number in row["gold"]), 0.0) for row in rows)
    return {"pairs": len(rows), "p_at_1": round(at(1), 3), "p_at_3": round(at(3), 3), "mrr": round(reciprocal / len(rows), 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--dense", action="store_true", help="with vectors (needs the embed worker); default: words and names alone")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    labels = json.loads((args.workdir / "labels-external.json").read_text())
    rows: list[dict] = []
    notes: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="aleph-decisions-") as scratch:
        for name, spec in labels["corpora"].items():
            root = args.workdir / name
            numbers = decisions(root, spec["sha"], spec["adr_dir"])
            engine = Engine(scratch_repository(root, spec["sha"], numbers, Path(scratch) / name), dense=args.dense)
            if args.dense:
                report = engine.embed()
                notes[name] = f"embedded {getattr(report, 'embedded', '?')} chunks"
            for pair in spec["pairs"]:
                query = task_text(root, pair["sha"])
                order, tokens = ranked(engine, query, numbers)
                rows.append({"corpus": name, "sha": pair["sha"], "gold": set(pair["gold"]), "order": order, "tokens": tokens,
                             "restates_title": pair["restates_title"], "agent_authored": pair["agent_authored"],
                             "query_words": len(query.split())})
            engine.close()
    report = {
        "arm": "words, names and vectors" if args.dense else "words and names",
        "all": score(rows), "oxn_bm25": OXN,
        "not restating the title": score([row for row in rows if not row["restates_title"]]),
        "not written by an agent": score([row for row in rows if not row["agent_authored"]]),
        "per corpus": {name: score([row for row in rows if row["corpus"] == name]) for name in labels["corpora"]},
        "mean tokens of the answer": round(sum(row["tokens"] for row in rows) / max(1, len(rows))),
        "notes": notes,
        "misses_at_3": [{"corpus": row["corpus"], "sha": row["sha"][:10], "gold": sorted(row["gold"]), "first": row["order"][:5]}
                        for row in rows if not any(number in row["gold"] for number in row["order"][:3])],
    }
    print(json.dumps({key: value for key, value in report.items() if key != "misses_at_3"}, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
