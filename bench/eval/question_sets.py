"""Questions for Benchmark C from sets other than Loc-Bench, so that languages other than Python get a verdict.

Loc-Bench is Python only. Three public sets hold issues with their fixes for other languages:

- `multilingual`: SWE-bench Multilingual (`SWE-bench/SWE-bench_Multilingual`, 300 questions, 9 languages)
- `polybench`:    SWE-PolyBench (`AmazonScience/SWE-PolyBench`, 2,110: Java, JavaScript, TypeScript, Python)
- `multi-swe`:    Multi-SWE-bench (`ByteDance-Seed/Multi-SWE-bench`, 1,632: C, C++, Go, Java, JavaScript, Rust, TypeScript)

What a question is, written down on 2026-10-12 before any of them was run (agy and the advisor consulted):

- the question is the issue's text. For Multi-SWE-bench, which has no such field, the title and body of every issue
  the fix resolves; never the pull request's own title or body, which describe the answer. A fix that resolves no
  issue gives no question.
- the answer is the source files the fix changed that were there before it: modified, deleted, or the old path of a
  rename, in a language Aleph has a profile for. Read from the fix's patch, never from the patch of its tests. Files
  the fix adds, lock files, changelogs, documentation and binaries are not answers; a fix with no answer left is no
  question. This is what Loc-Bench's answers are too (the files of the functions a fix edited).
- a question whose fix changed more than ten such files is left out: ten files are what is looked at.
- the same fix in two sets (the same repository, base commit and answer) is asked once.
- no repository gives more than `per_repo` questions to a language (drawn by a fixed seed), so that one large project
  does not decide a language's verdict. A language is read only with at least four repositories.
- a question's language is the set's own label where it has one, else the language most of its answer is written in.
- questions whose text names one of its answer's paths are kept (issues do that) and marked, so they can be read apart.
"""
from __future__ import annotations

import codecs
import json
import random
import re
from pathlib import Path

from aleph_zero import lang

SEED = 20261012
MOST_FILES = 10
REPOSITORIES_FOR_A_VERDICT = 4
HUB = {"multilingual": "SWE-bench/SWE-bench_Multilingual", "polybench": "AmazonScience/SWE-PolyBench", "multi-swe": "ByteDance-Seed/Multi-SWE-bench"}
_LABELS = {"js": "javascript", "ts": "typescript", "tsx": "typescript", "c++": "cpp", "golang": "go"}
_KNOWN = frozenset({"python", "typescript", "javascript", "go", "rust", "java", "cpp", "c"})
_HEADER = re.compile(r'^diff --git (?:"a/((?:[^"\\]|\\.)*)"|a/(.*?)) (?:"b/((?:[^"\\]|\\.)*)"|b/(.*))$')


def _unquoted(path: str) -> str:
    """A path as git writes it between quotes (`caf\\303\\251.rs`), read back."""
    return codecs.escape_decode(path.encode("latin-1", "replace"))[0].decode("utf-8", "replace")


def _side(line: str, mark: str) -> str | None:
    """The path on a `--- ` or `+++ ` line; None for /dev/null."""
    rest = line[4:].rstrip("\n").split("\t")[0]
    if rest.startswith('"'):
        rest = _unquoted(rest.strip('"'))
    return rest[2:] if rest.startswith(mark + "/") else None


def gold_of(patch: str) -> list[str]:
    """The source files a patch changes that were there before it, sorted."""
    found: set[str] = set()
    old: str | None = None
    added = False

    def close() -> None:
        if old and not added and lang.profile_for_path(old) is not None:
            found.add(old)

    for line in (patch or "").split("\n"):
        if line.startswith("diff --git "):
            close()
            added = False
            header = _HEADER.match(line)
            if header is None:
                old = None
            elif header.group(1) is not None:
                old = _unquoted(header.group(1))
            else:       # `a/P b/Q`, where a path may hold a space: when both sides are one path it is half of what is there
                rest = line[len("diff --git "):]
                half = (len(rest) - 5) // 2
                old = rest[2:2 + half] if rest == f"a/{rest[2:2 + half]} b/{rest[2:2 + half]}" else header.group(2)
        elif line.startswith("new file mode"):
            added = True
        elif line.startswith("rename from "):
            name = line[len("rename from "):]
            old = _unquoted(name.strip('"')) if name.startswith('"') else name
        elif line.startswith("--- ") and old is not None:
            before = _side(line, "a")
            if line[4:].startswith("/dev/null"):
                added = True
            elif before is not None:
                old = before
    close()
    return sorted(found)


def language_of(paths: list[str]) -> str:
    """The language most of these files are written in ("" when none is source code). A `.h` follows the sources it is with."""
    counts: dict[str, int] = {}
    headers = 0
    for path in paths:
        profile = lang.profile_for_path(path)
        if profile is None:
            continue
        if path.lower().endswith(".h"):
            headers += 1
        else:
            name = _LABELS.get(profile.name, profile.name)
            counts[name] = counts.get(name, 0) + 1
    if counts:
        return min(counts, key=lambda name: (-counts[name], name))
    return "cpp" if headers else ""


def _label(given) -> str:
    name = str(given or "").strip().lower()
    return _LABELS.get(name, name)


def _question(repo: str, commit: str, instance: str, statement: str, patch: str, label: str, name: str) -> dict | None:
    gold = gold_of(patch)
    language = _label(label) or language_of(gold)
    if not gold or not statement.strip() or not commit or language not in _KNOWN:
        return None
    return {"repo": repo, "base_commit": commit, "instance_id": instance, "problem_statement": statement, "gold": gold,
            "language": language, "set": name, "names_a_gold_path": any(path in statement for path in gold)}


def from_swebench(row: dict, name: str) -> dict | None:
    """A row in SWE-bench's shape (SWE-bench Multilingual, SWE-PolyBench)."""
    return _question(row["repo"], row.get("base_commit") or "", row["instance_id"], row.get("problem_statement") or "",
                     row.get("patch") or "", row.get("language"), name)


def from_multi_swe(row: dict, language: str) -> dict | None:
    """A row of Multi-SWE-bench, from the directory of its language."""
    issues = row.get("resolved_issues") or []
    statement = "\n\n".join(part.strip() for issue in issues for part in (issue.get("title"), issue.get("body")) if part and part.strip())
    return _question(f"{row['org']}/{row['repo']}", (row.get("base") or {}).get("sha") or "", f"{row['org']}__{row['repo']}-{row['number']}",
                     statement, row.get("fix_patch") or "", language, "multi-swe")


def select(rows: list[dict], per_repo: int | None, language: str | None = None, seed: int = SEED,
           asked: frozenset[str] | set[str] = frozenset()) -> tuple[list[dict], dict[str, int]]:
    """The questions that are asked, in the order given, and how many were left out, by reason. With `asked` (the
    names of questions an earlier run scored) it is a second draw: only questions never asked, and none that is one
    of those under another number."""
    left: dict[str, int] = {}

    def leave(why: str, count: int = 1) -> None:
        left[why] = left.get(why, 0) + count

    seen: set[tuple] = set()
    by_repo: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if language is not None and row["language"] != language:
            continue
        repo, gold = row["repo"].lower(), tuple(row["gold"])
        key = (repo, row["base_commit"].lower(), gold)
        # Two sets give the same pull request at commits of their own, and an issue answered twice (a backport) has
        # two numbers: the same question either way, and counting it twice counts its repository's luck twice.
        elsewhere = [(repo, "fix", row["instance_id"].lower()), (repo, "words", gold, " ".join(row["problem_statement"].split()))]
        if key in seen:
            leave("asked already in another set")
        elif any(other in seen for other in elsewhere):
            leave("asked already in another set" if elsewhere[0] in seen else "asked already at another commit")
        elif len(row["gold"]) > MOST_FILES:
            leave("more than ten files changed")
        elif row["instance_id"] in asked:
            leave("asked in an earlier run")
            seen.add(key)
            seen.update(elsewhere)
        else:
            seen.add(key)
            seen.update(elsewhere)
            by_repo.setdefault((row["language"], row["repo"].lower()), []).append(row)
    kept: set[int] = set()
    for (in_language, repo), some in by_repo.items():
        if per_repo is not None and len(some) > per_repo:
            leave("over the limit for one repository", len(some) - per_repo)
            some = random.Random(f"{seed}:{in_language}:{repo}").sample(sorted(some, key=lambda row: row["instance_id"]), per_repo)
        kept.update(id(row) for row in some)
    return [row for row in rows if id(row) in kept], left


def counts(rows: list[dict]) -> dict[str, dict]:
    """Per language: questions, repositories, and whether that is enough to read a verdict from."""
    out: dict[str, dict] = {}
    for row in rows:
        entry = out.setdefault(row["language"], {"questions": 0, "repositories": set(), "name a path of their answer": 0})
        entry["questions"] += 1
        entry["repositories"].add(row["repo"].lower())
        entry["name a path of their answer"] += row["names_a_gold_path"]
    return {language: {**entry, "repositories": len(entry["repositories"]), "enough repositories": len(entry["repositories"]) >= REPOSITORIES_FOR_A_VERDICT}
            for language, entry in sorted(out.items())}


# ---- reading the sets (network: each is downloaded once into `cache`) ---------------------------------

def load(name: str, cache: Path) -> list[dict]:
    """Every usable question of one set, before `select`."""
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download, list_repo_files

    out: list[dict] = []
    if name in ("multilingual", "polybench"):
        path = hf_hub_download(HUB[name], "default/test/0000.parquet", repo_type="dataset", revision="refs/convert/parquet", local_dir=cache / name)
        out = [from_swebench(row, name) for row in pq.read_table(path).to_pylist()]
    elif name == "multi-swe":
        for file in sorted(list_repo_files(HUB[name], repo_type="dataset")):
            directory, _, rest = file.partition("/")
            language = _label(directory)
            if not rest.endswith(".jsonl") or language not in _KNOWN or language == "python":
                continue
            with open(hf_hub_download(HUB[name], file, repo_type="dataset", local_dir=cache / name), encoding="utf-8", errors="replace") as handle:
                out += [from_multi_swe(json.loads(line), language) for line in handle if line.strip()]
    else:
        raise ValueError(f"no such set as {name}: {', '.join(HUB)}")
    return [row for row in out if row is not None]
