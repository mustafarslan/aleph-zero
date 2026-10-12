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
import math
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from aleph_zero import decisions as decision_records
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
_NUMBERED = re.compile(r"(?:ADR[-_])?(\d+)[-_]", re.I)   # oxn's `0012-…`; the optional prefix is for development corpora
K = 60      # chunks asked for: enough for every decision of a corpus to appear


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def task_text(root: Path, sha: str) -> str:
    """What a retriever would be given: the commit, minus the answer and the plumbing."""
    subject, _, body = git(root, "log", "-1", "--format=%s\x1f%b", sha).partition("\x1f")
    return NOISE.sub(" ", CITATION.sub(" ", f"{subject.strip()}\n{body}")).strip()


def decisions(root: Path, sha: str, adr_dir: str) -> dict[str, int]:
    """path -> the number a citation would name, for each numbered decision at the pinned commit:
    the digits of the file name up to its first hyphen, as oxn's parser has it (`0012-use-http2.md` is 12)."""
    found = {}
    for path in git(root, "ls-tree", "--name-only", sha, adr_dir.rstrip("/") + "/").split("\n"):
        name = Path(path).name
        numbered = _NUMBERED.match(name)
        if path.endswith(".md") and numbered is not None:
            found[path] = int(numbered.group(1))
    return found


# ---- the reference arm: oxn's own ranker, to know the instrument measures what oxn measured ----------

_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_TITLE = re.compile(r"^#\s+(?:\d+\.\s*)?(.+?)\s*$", re.MULTILINE)


def tokenize(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


def reference_order(texts: dict[str, str], numbers: dict[str, int], query: str, k1: float = 1.5, b: float = 0.75) -> list[int]:
    """oxn's BM25 (`context/bm25.py`, v0.1.2) over `title\n\nbody` of each decision (these corpora have no
    tags), ties broken by the decision's identifier."""
    documents = {}
    for path, body in texts.items():
        title = _TITLE.search(body)
        if title is None:
            continue        # oxn's parser takes no decision from a file with no heading
        documents[path] = Counter(tokenize(f"{title.group(1)}\n\n{body}"))
    lengths = {path: sum(terms.values()) for path, terms in documents.items()}
    average = sum(lengths.values()) / len(lengths) if lengths else 0.0
    postings: dict[str, dict[str, int]] = {}
    for path, terms in documents.items():
        for term, frequency in terms.items():
            postings.setdefault(term, {})[path] = frequency
    scores: dict[str, float] = {}
    for term in tokenize(query):
        found = postings.get(term)
        if found is None:
            continue
        weight = math.log(1 + (len(documents) - len(found) + 0.5) / (len(found) + 0.5))
        for path, frequency in found.items():
            norm = k1 * (1 - b + b * (lengths[path] / average if average else 1.0))
            scores[path] = scores.get(path, 0.0) + weight * frequency * (k1 + 1) / (frequency + norm)
    ranked = sorted(scores.items(), key=lambda hit: (-hit[1], f"{numbers[hit[0]]:04d}"))
    return [numbers[path] for path, _ in ranked]


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
    parser.add_argument("--reference", action="store_true", help="oxn's own ranker over the same candidates: calibrates this harness")
    parser.add_argument("--ranker", choices=("words", "fused5", "fused60", "stems", "length", "stems+length"),
                        help="Aleph's ranker for decisions (`Engine.decisions`): by words alone, fused with vectors at that k, "
                             "or one of Task 2b's candidates: words reduced to stems, b = 0.4, both")
    parser.add_argument("--labels", default="labels-external.json", help="the pairs, a file in the workdir (default: oxn's 100)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.ranker in ("stems", "stems+length"):
        decision_records.STEMS = True
    if args.ranker in ("length", "stems+length"):
        decision_records.B = 0.4
    labels = json.loads((args.workdir / args.labels).read_text())
    rows: list[dict] = []
    notes: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="aleph-decisions-") as scratch:
        for name, spec in labels["corpora"].items():
            root = args.workdir / name
            numbers = decisions(root, spec["sha"], spec["adr_dir"])
            where = scratch_repository(root, spec["sha"], numbers, Path(scratch) / name)
            if args.reference:
                texts = {path: (where / path).read_text(errors="replace") for path in numbers}
                for pair in spec["pairs"]:
                    query = task_text(root, pair["sha"])
                    rows.append({"corpus": name, "sha": pair["sha"], "gold": set(pair["gold"]), "order": reference_order(texts, numbers, query),
                                 "tokens": 0, "restates_title": pair["restates_title"], "agent_authored": pair["agent_authored"]})
                continue
            fused = args.ranker in ("fused5", "fused60")
            engine = Engine(where, dense=args.dense or fused)
            if args.dense or fused:
                report = engine.embed()
                notes[name] = f"embedded {getattr(report, 'embedded', '?')} chunks"
            for pair in spec["pairs"]:
                query = task_text(root, pair["sha"])
                if args.ranker:
                    answer = engine.decisions(query, k=len(numbers), rrf_k={"fused5": 5, "fused60": 60}.get(args.ranker, 0))
                    order, tokens = [hit.number for hit in answer.decisions], estimate_tokens(decision_records.text(
                        decision_records.Answer(query, answer.decisions[:7], answer.known)))
                else:
                    order, tokens = ranked(engine, query, numbers)
                rows.append({"corpus": name, "sha": pair["sha"], "gold": set(pair["gold"]), "order": order, "tokens": tokens,
                             "restates_title": pair["restates_title"], "agent_authored": pair["agent_authored"],
                             "query_words": len(query.split())})
            engine.close()
    report = {
        "arm": f"Aleph's decision ranker ({args.ranker})" if args.ranker else "oxn's BM25 (reference)" if args.reference else "words, names and vectors" if args.dense else "words and names",
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
