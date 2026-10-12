"""Citation-derived task -> decision pairs from a repository's own history: a development set.

Usage:
  uv run python bench/eval/decision_labels.py NAME=PATH:ADR_DIR[+ADR_DIR...] [...] --out FILE

The rule starts from oxn's (`scripts/extract_citation_labels.py`, v0.1.2, MIT, same author): a pair is a
commit that cites an ADR while doing the work it governs; the gold is the cited decision. Commits
that author the decision, citations of decisions absent at HEAD, and commits with fewer than three
content words left after stripping are excluded. No task text is stored: the evaluation reads it
from the clone.

On large public repositories that rule lets through commits that do no governed work. So, for
development sets, and written down on 2026-10-12 before any ranker was measured on them:

- a record is a file the engine itself takes for a decision record (`aleph_zero.decisions.is_record`),
  directly in one of the directories given, with the number and title the engine reads from it; a file
  named as a template is not one;
- the commit must change at least one file outside those directories: a typo fixed in a record cites
  it and is not work it governs;
- commits by a bot, and dependency bumps (`build(deps`, `Bump x from a to b`), are left out: their
  messages carry changelogs that name records.

oxn's 100 pairs are not drawn by this file and are untouched by it. The output has the shape of oxn's
`retrieval-labels-external.json`, with the records of each corpus written out (`records`), so that
the evaluation ranks exactly the candidates the pairs were drawn against.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from aleph_zero import decisions

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
BOT = re.compile(r"\[bot\]|dependabot|renovate|greenkeeper", re.I)
BUMP = re.compile(r"^build\(deps|^(chore\(deps[^)]*\):\s*)?bump \S+ from \S+ to \S+", re.I)
TEMPLATE = re.compile(r"template", re.I)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True).stdout


def words(text: str) -> set[str]:
    return {word.lower() for word in WORD.findall(text) if word.lower() not in STOP}


def records_in(root: Path, sha: str, adr_dirs: str) -> tuple[dict[str, int], dict[int, str]]:
    """path -> number, and number -> title, of the decision records directly in the directories given
    (`a+b` for two), as the engine reads them."""
    paths, titled = {}, {}
    for adr_dir in adr_dirs.split("+"):
        for path in git(root, "ls-tree", "--name-only", sha, adr_dir.rstrip("/") + "/").split("\n"):
            if not path or TEMPLATE.search(Path(path).name) or decisions.is_record(path) is None:
                continue
            record = decisions.parse(path, decisions.is_record(path), git(root, "show", f"{sha}:{path}"))
            paths[path], titled[record.number] = record.number, record.title
    return paths, titled


def pairs_in(root: Path, sha: str, adr_dirs: str, left_out: dict[str, int] | None = None) -> tuple[list[dict], dict[str, int]]:
    """The pairs, and the records they were drawn against. `left_out` counts what the rule excluded, by reason."""
    paths, known = records_in(root, sha, adr_dirs)
    inside = tuple(adr_dir.rstrip("/") + "/" for adr_dir in adr_dirs.split("+"))
    left = left_out if left_out is not None else {}

    def leave(why: str) -> None:
        left[why] = left.get(why, 0) + 1

    found = []
    for block in git(root, "log", "--no-merges", "--format=\x1e%H\x1f%an <%ae>\x1f%s\x1f%b", sha).split("\x1e")[1:]:
        parts = block.split("\x1f")
        if len(parts) < 4:
            continue
        commit, author, subject, body = parts[0], parts[1], parts[2].strip(), parts[3]
        cited = {int(number) for number in CITATION.findall(f"{subject} {body}")}
        if not cited:
            continue
        gold = sorted(cited & set(known))
        if not gold:
            leave("cites no record that is there")
        elif AUTHORING.search(subject):
            leave("writes the record")
        elif BOT.search(author) or BUMP.search(subject):
            leave("a bot or a dependency bump")
        elif len(text := words(NOISE.sub(" ", CITATION.sub(" ", f"{subject}\n{body}")).strip())) < 3:
            leave("fewer than three words left")
        elif not any(path and not path.startswith(inside) for path in git(root, "show", "--name-only", "--format=", commit).split("\n")):
            leave("only the records changed")
        else:
            repeated = max(len(words(known[number]) & text) / max(1, len(words(known[number]))) for number in gold)
            found.append({"sha": commit, "gold": gold, "restates_title": repeated >= RESTATES_TITLE,
                          "agent_authored": "Co-Authored-By: Claude" in body or "claude.ai/code" in body})
    return found, paths


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
        left: dict[str, int] = {}
        pairs, records = pairs_in(root, sha, adr_dir, left)
        corpora[name] = {"sha": sha, "adr_dir": adr_dir, "decisions": len(records), "records": records, "left_out": left, "pairs": pairs}
        print(f"{name}: {len(records)} decisions, {len(pairs)} pairs over {len({number for pair in pairs for number in pair['gold']})} of them "
              f"({sum(pair['restates_title'] for pair in pairs)} restate a title, {sum(pair['agent_authored'] for pair in pairs)} written by an agent); "
              f"left out: {', '.join(f'{count} {why}' for why, count in sorted(left.items())) or 'nothing'}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"rule": "oxn's citation rule, tightened for development sets (see bench/eval/decision_labels.py)", "corpora": corpora}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
