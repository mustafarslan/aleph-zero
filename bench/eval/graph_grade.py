"""How right are Aleph's call edges? Graded against a SCIP index, with oxn's protocol.

Usage:
  uv run python bench/eval/graph_grade.py bench/data/graph [--corpus python-httpx] [--out FILE]

The definitions are fixed in the M7 plan and come from oxn (`resolve/measure.py`, v0.1.2),
because the bars in ADR-003 are oxn's figures:

- population: call sites the SCIP index resolves to a definition inside the tree, where
  SCIP's symbol names what the source writes. Sites where the oracle's symbol contradicts
  the written name are excluded and counted.
- recall: the share of the population Aleph answers at all.
- precision: the share of its answers that name the definition SCIP names. A site with
  several candidates is answered with its first and counts once.
- confident: an answer with exactly one candidate. Confident precision is correct confident
  answers over confident answers; confident recall is confident answers over the population.

The answers come from a function `answer(path, call) -> list[Candidate]`; with none, the
grader reports the population alone (recall 0), which is how it was first run.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aleph_zero import lang, scip
from aleph_zero.graph import FileFacts, Graph
from aleph_zero.lang import parse
from aleph_zero.lang.calls import Call, calls
from aleph_zero.lang.definitions import definitions
from aleph_zero.lang.imports import extract_imports

CORPORA = {
    "python-httpx": "python", "go-kit": "go", "rust-ripgrep": "rust", "typescript-nest": "typescript",
    "java-spring-petclinic": "java", "javascript-eslint": "javascript",
}
# ADR-003: confident precision at least 99% everywhere; confident recall above oxn's where it reports one.
BARS = {"go": 0.641, "rust": 0.423, "typescript": 0.567}
_DESCRIPTOR = re.compile(r"(?:`([^`]+)`|([A-Za-z_$][\w$]*))(?:\([^)]*\))?[.#:/!]*$")


@dataclass(frozen=True)
class Candidate:
    """One definition a call may reach, as Aleph names it."""
    path: str
    name: str
    start_line: int   # 1-based, of the definition's whole range
    end_line: int


@dataclass(frozen=True)
class Truth:
    path: str
    line: int         # 0-based line of the definition's name, as SCIP gives it
    name: str         # '' for a symbol local to its file, whose name SCIP does not carry
    owner: str = ""   # for a constructor, the type it constructs: a call writes one name or the other

    def names(self) -> tuple[str, ...]:
        return tuple(name for name in (self.name, self.owner) if name)


@dataclass
class Grade:
    language: str
    graded: int = 0
    excluded_untrustworthy: int = 0
    answered: int = 0
    correct: int = 0
    confident_answered: int = 0
    confident_correct: int = 0
    disagreements: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        ratio = lambda part, whole: round(part / whole, 4) if whole else 0.0  # noqa: E731
        return {
            "language": self.language, "graded_call_sites": self.graded,
            "excluded_untrustworthy": self.excluded_untrustworthy,
            "excluded_share": ratio(self.excluded_untrustworthy, self.graded + self.excluded_untrustworthy),
            "answered": self.answered, "correct": self.correct,
            "recall": ratio(self.answered, self.graded), "precision": ratio(self.correct, self.answered),
            "confident_answered": self.confident_answered,
            "confident_precision": ratio(self.confident_correct, self.confident_answered),
            "confident_recall": ratio(self.confident_answered, self.graded),
        }


_CONSTRUCTORS = ("__init__", "<init>", "<constructor>", "constructor")
_OWNER = re.compile(r"(?:`([^`]+)`|([A-Za-z_$][\w$]*))#(?:`?<?[A-Za-z_]+>?`?)\([^)]*\)\.$")
_ACCESSOR = re.compile(r"^<(?:get|set)>")


def descriptor_name(symbol: str) -> str:
    """The name a SCIP symbol ends in: `... pkg/a/Client#send().` -> `send`; an accessor
    `<get>size` -> `size`. '' for a symbol local to its file."""
    if symbol.startswith("local "):
        return ""
    match = _DESCRIPTOR.search(symbol.rsplit(" ", 1)[-1])
    return _ACCESSOR.sub("", (match.group(1) or match.group(2))) if match else ""


def constructed_type(symbol: str) -> str:
    """For a constructor's symbol, the type it constructs (`Client#__init__().` -> `Client`):
    `Client(...)` writes the type, `super().__init__(...)` writes the method. Else ''."""
    if descriptor_name(symbol) not in _CONSTRUCTORS:
        return ""
    owner = _OWNER.search(symbol.rsplit(" ", 1)[-1])
    return (owner.group(1) or owner.group(2)) if owner else ""


def definition_sites(index: scip.ScipIndex) -> dict[tuple[str, str], Truth]:
    """Where each symbol is defined in the tree. A symbol local to a file is keyed with its file."""
    found: dict[tuple[str, str], Truth] = {}
    for document in index.documents:
        for occurrence in document.definitions():
            scope = document.relative_path if occurrence.symbol.startswith("local ") else ""
            found.setdefault((scope, occurrence.symbol),
                             Truth(document.relative_path, occurrence.start_line, descriptor_name(occurrence.symbol),
                                   constructed_type(occurrence.symbol)))
    return found


def is_correct(candidate: Candidate, truth: Truth) -> bool:
    return (candidate.path == truth.path and candidate.start_line - 1 <= truth.line <= candidate.end_line - 1
            and (not truth.name or candidate.name in truth.names()))


def grade_file(grade: Grade, path: str, source: bytes, document: scip.ScipDocument, sites: dict[tuple[str, str], Truth],
               answer: Callable[[str, Call], list[Candidate]] | None) -> None:
    profile = lang.profile_for_path(path)
    if profile is None:
        return
    tree = parse.tree(profile, source)
    found = calls(profile, tree, source, definitions(profile, tree, source))
    at = {(occurrence.start_line, occurrence.start_char): occurrence for occurrence in document.occurrences
          if not occurrence.is_definition}
    for call in found:
        occurrence = at.get((call.line - 1, call.column))
        if occurrence is None:
            continue  # SCIP resolved nothing here
        scope = path if occurrence.symbol.startswith("local ") else ""
        truth = sites.get((scope, occurrence.symbol))
        if truth is None:
            continue  # defined outside the tree: a library, a builtin
        if truth.name and call.name not in truth.names():
            grade.excluded_untrustworthy += 1  # the oracle names something other than what is written
            continue
        grade.graded += 1
        candidates = answer(path, call) if answer is not None else []
        if not candidates:
            continue
        grade.answered += 1
        right = is_correct(candidates[0], truth)
        grade.correct += right
        if len(candidates) == 1:
            grade.confident_answered += 1
            grade.confident_correct += right
        if not right and len(grade.disagreements) < 40:
            grade.disagreements.append(f"{path}:{call.line} {call.receiver + '.' if call.receiver else ''}{call.name} -> "
                                       f"{candidates[0].path}:{candidates[0].start_line} but SCIP says {truth.path}:{truth.line + 1}")


def grade_corpus(root: Path, index: scip.ScipIndex, language: str,
                 answer: Callable[[str, Call], list[Candidate]] | None = None) -> Grade:
    grade = Grade(language)
    sites = definition_sites(index)
    families = {language, "tsx"} if language == "typescript" else {language}
    for document in index.documents:
        profile = lang.profile_for_path(document.relative_path)
        source = root / document.relative_path
        if profile is None or profile.name not in families or not source.is_file():
            continue
        grade_file(grade, document.relative_path, source.read_bytes(), document, sites, answer)
    return grade


def tracked_files(root: Path) -> list[str]:
    """The corpus as Git sees it: what a view of it would hold."""
    listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [name for name in listed.decode("utf-8", "surrogateescape").split("\0") if name]


def aleph_graph(root: Path) -> Graph:
    """Aleph's graph of a corpus on disk, from the same facts the index would hold per file."""
    facts: dict[str, FileFacts] = {}
    for path in tracked_files(root):
        profile = lang.profile_for_path(path)
        file = root / path
        if profile is None or not file.is_file() or file.is_symlink():
            continue
        data = file.read_bytes()
        tree = parse.tree(profile, data)
        facts[path] = FileFacts(profile.name, definitions(profile, tree, data), extract_imports(tree.root_node, profile))
    return Graph(facts)


def aleph_answer(graph: Graph) -> Callable[[str, Call], list[Candidate]]:
    def answer(path: str, call: Call) -> list[Candidate]:
        edge = graph.resolve(path, call)
        return [] if edge is None else [Candidate(t.path, t.name, t.start_line, t.end_line) for t in edge.targets]
    return answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path, help="holds <corpus>/ and <corpus>.scip for each corpus")
    parser.add_argument("--corpus", action="append", choices=sorted(CORPORA))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--population", action="store_true", help="report the population alone, with no answers")
    parser.add_argument("--disagreements", type=int, default=0, help="print this many wrong answers per corpus")
    args = parser.parse_args(argv)
    report = {}
    for name in args.corpus or sorted(CORPORA):
        root, index_file = args.workdir / name, args.workdir / f"{name}.scip"
        if not root.is_dir() or not index_file.is_file():
            print(f"{name}: not prepared under {args.workdir} (the corpus or its .scip is missing)", file=sys.stderr)
            continue
        index = scip.load_index(index_file)
        answer = None if args.population else aleph_answer(aleph_graph(root))
        grade = grade_corpus(root, index, CORPORA[name], answer)
        report[name] = {**grade.as_dict(), "documents": len(index.documents), "oxn_confident_recall": BARS.get(CORPORA[name])}
        row = report[name]
        print(f"{name:24} graded {row['graded_call_sites']:6}  excluded {row['excluded_untrustworthy']:5} "
              f"({row['excluded_share']:.1%})  recall {row['recall']:.1%}  precision {row['precision']:.1%}  "
              f"confident precision {row['confident_precision']:.1%}  confident recall {row['confident_recall']:.1%}", flush=True)
        for line in grade.disagreements[:args.disagreements]:
            print("   ", line)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
