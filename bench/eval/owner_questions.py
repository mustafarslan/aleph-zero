"""The owner's own questions for Benchmark C (DEFERRED entry 2): how they are made and checked, so that nobody who
built Aleph writes or edits one.

Made on 2026-10-15 at the owner's word ("fifty test with advisor and agy help"), by a protocol the advisor proposed
and agy argued against before any question existed:

- repositories: the owner's public ones, never Aleph's own (where the author's bias is greatest), each at the
  commit its local clone had when the file was made;
- `identifier`: drawn here, by a fixed seed, from the names a repository defines in exactly one file and mentions in
  three files or more (agy: a bare name defined once and used nowhere is a straw man, since the definition is first
  by rule; a name used elsewhere must be told from its uses). The question is the name; the answer its file;
- `conceptual`, `mixed`, `documentation`: drafted by agy blind, from the repository's README, documentation and
  commit titles, never its source (agy: a question written after reading its answer leaks the answer's words); the
  files that answer them are named in a separate pass, by an agent that knows nothing of Aleph, reading the code;
- checks are mechanical (`check`); what fails is dropped, never edited.

The questions are not read by the owner unless they choose to; the file holds one line a question so any can be struck.
"""
from __future__ import annotations

import json
import random
import re
import subprocess
from pathlib import Path

SEED = 20261015
PROJECTS = Path("/Volumes/AI_SSD/Projects")
REPOSITORIES = {"aeon": "mustafarslan/aeon", "oxn": "mustafarslan/oxn", "janus": "mustafarslan/janus",
                "fidus": "mustafarslan/fidus", "nexus": "mustafarslan/nexus", "carbon": "mustafarslan/carbon"}

# What a definition looks like, per file suffix: the name is the regex's last group.
DEFINITIONS = {
    ".py": re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)"),
    ".go": re.compile(r"^(?:func\s+(?:\([^)]*\)\s*)?|type\s+)([A-Za-z_][A-Za-z0-9_]*)"),
    ".java": re.compile(r"^\s*(?:public|protected|private|abstract|final|static|\s)*(?:class|interface|enum|record)\s+([A-Za-z_][A-Za-z0-9_]*)"),
    ".hpp": re.compile(r"^\s*(?:class|struct)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:final\s*)?[:{]"),
    ".cpp": re.compile(r"^\s*(?:class|struct)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:final\s*)?[:{]"),
    ".h": re.compile(r"^\s*(?:class|struct)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:final\s*)?[:{]"),
}
TESTS = re.compile(r"(^|/)(tests?|testing|__tests__|spec)(/|$)|(^|/)test_[^/]*$|_test\.(go|py)$|Test[^/]*\.java$")
COMPOUND = re.compile(r"[A-Za-z][a-z0-9]+(?:[A-Z][a-z0-9]+)+|[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def head(name: str) -> str:
    return _git(PROJECTS / name, "rev-parse", "HEAD").strip()


VENDORED = re.compile(r"(^|/)(external|vendor|third_party|thirdparty|node_modules|deps)(/|$)")


def files_at(name: str, commit: str) -> list[str]:
    """The files (not submodules) at `commit`, without vendored code: the owner's own."""
    rows = _git(PROJECTS / name, "ls-tree", "-r", commit).splitlines()
    return [row.split("\t", 1)[1] for row in rows if row.split()[1] == "blob" and not VENDORED.search(row.split("\t", 1)[1])]


def definitions(name: str, commit: str) -> dict[str, set[str]]:
    """Each name a source file (not a test) defines at `commit`, with the files that define it."""
    found: dict[str, set[str]] = {}
    for path in files_at(name, commit):
        pattern = DEFINITIONS.get(Path(path).suffix)
        if pattern is None or TESTS.search(path):
            continue
        text = _git(PROJECTS / name, "show", f"{commit}:{path}")
        for line in text.splitlines():
            match = pattern.match(line)
            if match:
                found.setdefault(match.group(1), set()).add(path)
    return found


def mentioned_in(name: str, commit: str, word: str) -> int:
    done = subprocess.run(["git", "-C", str(PROJECTS / name), "grep", "-l", "-w", "-F", word, commit],
                          capture_output=True, text=True)
    return len(done.stdout.splitlines())


def identifier_questions(per_repo: int = 3) -> list[dict]:
    """Names defined in exactly one file and mentioned in three or more, at least eight characters, not private."""
    chosen = random.Random(SEED)
    rows = []
    for name, repo in REPOSITORIES.items():
        commit = head(name)
        candidates = sorted(word for word, where in definitions(name, commit).items()
                            if len(where) == 1 and len(word) >= 8 and not word.startswith("_") and not word.lower().startswith("test"))
        chosen.shuffle(candidates)
        taken = 0
        for word in candidates:
            if taken == per_repo:
                break
            if mentioned_in(name, commit, word) < 3:
                continue
            path = next(iter(definitions_cache(name, commit)[word]))
            rows.append(_row(name, repo, commit, f"{name}-identifier-{taken + 1}", word, [path], "identifier"))
            taken += 1
    return rows


_DEFINED: dict[tuple[str, str], dict[str, set[str]]] = {}


def definitions_cache(name: str, commit: str) -> dict[str, set[str]]:
    if (name, commit) not in _DEFINED:
        _DEFINED[name, commit] = definitions(name, commit)
    return _DEFINED[name, commit]


def _row(name: str, repo: str, commit: str, instance: str, statement: str, gold: list[str], category: str) -> dict:
    return {"repo": repo, "base_commit": commit, "instance_id": instance, "problem_statement": statement, "gold": sorted(gold),
            "language": _language(gold), "category": category, "local": str(PROJECTS / name), "set": "owner",
            "names_a_gold_path": any(path in statement for path in gold)}


def _language(gold: list[str]) -> str:
    suffixes = {".py": "python", ".go": "go", ".java": "java", ".cpp": "cpp", ".hpp": "cpp", ".h": "cpp", ".ts": "typescript",
                ".tsx": "typescript", ".js": "javascript"}
    kinds = [suffixes.get(Path(path).suffix, "docs") for path in gold]
    return max(set(kinds), key=kinds.count)


def check(row: dict) -> str | None:
    """Why a question is dropped, or None. Mechanical: never a judgement of the question's worth."""
    name = Path(row["local"]).name
    present = set(files_at(name, row["base_commit"]))
    missing = [path for path in row["gold"] if path not in present]
    if not row["gold"] or missing:
        return f"answer not in the commit: {missing or 'none named'}"
    if len(row["gold"]) > 3:
        return "more than three answering files"
    if row["category"] == "identifier":
        where = definitions_cache(name, row["base_commit"]).get(row["problem_statement"], set())
        if set(row["gold"]) != where:
            return "the name is not defined in exactly its answer"
    if row["category"] == "conceptual":
        # A whole compound identifier of an answer in the question: by name, not by concept. Plain words are kept.
        named = {word for path in row["gold"] for word in COMPOUND.findall(_git(PROJECTS / name, "show", f"{row['base_commit']}:{path}"))}
        leaked = [word for word in COMPOUND.findall(row["problem_statement"]) if word in named]
        if leaked:
            return f"names its answer's identifiers: {leaked}"
    if row["category"] == "documentation" and not all(Path(path).suffix in (".md", ".rst", ".txt", ".adoc") for path in row["gold"]):
        return "a documentation question answered by code"
    return None


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["identifiers"]:
        print(json.dumps(identifier_questions(), indent=1))
