"""Citation-derived task -> decision pairs from a repository's own history, by oxn's rule.

Usage:
  uv run python bench/eval/decision_labels.py NAME=PATH:ADR_DIR [NAME=PATH:ADR_DIR ...] --out FILE

The rule is oxn's (`scripts/extract_citation_labels.py`, v0.1.2, MIT, same author): a pair is a
commit that cites an ADR while doing the work it governs; the gold is the cited decision. Commits
that author the decision, citations of decisions absent at HEAD, and commits with fewer than three
content words left after stripping are excluded. No task text is stored: the evaluation reads it
from the clone. The output has the shape of oxn's `retrieval-labels-external.json`.

Used to make a development set from repositories that are not among oxn's five, so that what is
built for decision retrieval is not shaped on the pairs it is graded on.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CITATION = re.compile(r"\bADR[- _]?(\d{1,4})\b", re.I)
AUTHORING = re.compile(
    r"\b(add|accept|record|refine|reword|mark|supersede[sd]?|amend|draft|propose|create"
    r"|write|rename)\b[^.]{0,40}\badr\b|^docs?[:(]|\badr[- _]?\d+\s*:",
    re.I,
)
NOISE = re.compile(
    r"^\w+(\([^)]*\))?!?:|\(#\d+\)|#\d+|\b[A-Z]{2,}-\d+\b"
    r"|^\s*(Co-Authored-By|Closes|Fixes|Refs|Signed-off-by):.*$|https?://\S+",
    re.M | re.I,
)
WORD = re.compile(r"[A-Za-z]{3,}")
STOP = frozenset(["the", "and", "for", "with", "from", "into", "that", "this", "per", "see", "ref", "adr"])
RESTATES_TITLE = 0.6
NUMBERED = re.compile(r"^(?:ADR[-_])?(\d+)[-_]", re.I)     # oxn's `0012-…`, and this repository's `ADR-012-…`
TITLE = re.compile(r"^#\s+(?:(?:ADR[- ]?)?\d+[.:]\s*)?(.+?)\s*$", re.MULTILINE | re.I)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout


def words(text: str) -> set[str]:
    return {word.lower() for word in WORD.findall(text) if word.lower() not in STOP}


def titles(root: Path, sha: str, adr_dir: str) -> dict[int, str]:
    found = {}
    for path in git(root, "ls-tree", "--name-only", sha, adr_dir.rstrip("/") + "/").split("\n"):
        numbered = NUMBERED.match(Path(path).name)
        if not path.endswith(".md") or numbered is None:
            continue
        title = TITLE.search(git(root, "show", f"{sha}:{path}"))
        if title is not None:
            found[int(numbered.group(1))] = title.group(1)
    return found


def pairs_in(root: Path, sha: str, adr_dir: str) -> tuple[list[dict], int]:
    known = titles(root, sha, adr_dir)
    found = []
    for block in git(root, "log", "--no-merges", "--format=\x1e%H\x1f%s\x1f%b", sha).split("\x1e")[1:]:
        parts = block.split("\x1f")
        if len(parts) < 3:
            continue
        commit, subject, body = parts[0], parts[1].strip(), parts[2]
        gold = sorted({int(number) for number in CITATION.findall(f"{subject} {body}")} & set(known))
        if not gold or AUTHORING.search(subject):
            continue
        text = words(NOISE.sub(" ", CITATION.sub(" ", f"{subject}\n{body}")).strip())
        if len(text) < 3:
            continue
        repeated = max(len(words(known[number]) & text) / max(1, len(words(known[number]))) for number in gold)
        found.append({"sha": commit, "gold": gold, "restates_title": repeated >= RESTATES_TITLE,
                      "agent_authored": "Co-Authored-By: Claude" in body or "claude.ai/code" in body})
    return found, len(known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpora", nargs="+", help="NAME=PATH:ADR_DIR")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    corpora = {}
    for given in args.corpora:
        name, _, rest = given.partition("=")
        path, _, adr_dir = rest.rpartition(":")
        root = Path(path)
        sha = git(root, "rev-parse", "HEAD").strip()
        pairs, known = pairs_in(root, sha, adr_dir)
        corpora[name] = {"sha": sha, "adr_dir": adr_dir, "decisions": known, "pairs": pairs}
        print(f"{name}: {known} decisions, {len(pairs)} pairs ({sum(pair['restates_title'] for pair in pairs)} restate a title, "
              f"{sum(pair['agent_authored'] for pair in pairs)} written by an agent)")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"rule": "oxn's citation rule, on repositories outside its five: a development set", "corpora": corpora}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
