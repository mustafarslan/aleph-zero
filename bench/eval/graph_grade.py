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

Reported beside these and outside oxn's protocol, so it decides no bar: what Aleph answers
at call sites whose callee SCIP places in another package than any this index defines (a
library, the standard library). Every such answer is a wrong edge the figures above cannot
see, since those sites are not graded. Sites SCIP could not resolve at all (a symbol local
to the file with no definition there) are left out: they say nothing either way.

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
from aleph_zero.graph import FEATURES, MANIFESTS, FileFacts, Graph
from aleph_zero.lang import parse
from aleph_zero.lang.bindings import bases, bindings, fields
from aleph_zero.lang.calls import Call, calls
from aleph_zero.lang.definitions import definitions

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
    how: str = ""     # how the answer was found (level and provenance); grouped in the report, decides nothing
    certain: bool = False   # Aleph's own stamp: one target, and not by its name alone


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
    outside: int = 0                # sites whose callee SCIP places outside the tree
    outside_answered: int = 0       # ... that Aleph answered anyway: wrong edges
    outside_confident: int = 0      # ... with one candidate
    outside_examples: list[str] = field(default_factory=list)
    certain_answered: int = 0       # graded sites answered with Aleph's own "certain" stamp
    certain_correct: int = 0
    outside_certain: int = 0        # outside sites answered with that stamp: wrong, every one
    packages: set[str] = field(default_factory=set)   # the packages this index defines symbols of
    disagreements: list[str] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)
    by_how: dict[str, list[int]] = field(default_factory=dict)  # 'how, confident|several' -> [answered, correct]
    keep: int = 40                # how many wrong answers to keep
    confident_only: bool = False  # keep only wrong answers that had one candidate

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
            "outside_sites": self.outside, "outside_answered": self.outside_answered,
            "outside_confident": self.outside_confident,
            "confident_wrong_edge_share": ratio(self.outside_confident + self.confident_answered - self.confident_correct,
                                                self.outside_confident + self.confident_answered),
            "certain_answered": self.certain_answered, "outside_certain": self.outside_certain,
            "certain_precision_all_sites": ratio(self.certain_correct, self.certain_answered + self.outside_certain),
            "certain_recall": ratio(self.certain_answered, self.graded),
            "by_how": {how: {"answered": answered, "correct": correct, "precision": ratio(correct, answered)}
                       for how, (answered, correct) in sorted(self.by_how.items())},
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


def package_of(symbol: str) -> str:
    """`scheme manager package version descriptors` -> 'manager package'; '' for a symbol local to its file."""
    parts = symbol.split(" ", 4)
    return "" if symbol.startswith("local ") or len(parts) < 5 else f"{parts[1]} {parts[2]}"


def _written(call: Call) -> str:
    receiver = " ".join(call.receiver.split())
    return f"{receiver[:40]}.{call.name}" if receiver else call.name


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
        # SCIP counts columns in the unit its document says; Aleph in bytes. They differ only on a line with
        # non-ASCII text before the call. Where the document names no unit, the indexer's habit is tried
        # first (Go and Rust tools count bytes, the others UTF-16 units), then the other units.
        in_units = {1: call.column, 2: call.column16 if call.column16 >= 0 else call.column,
                    3: call.column32 if call.column32 >= 0 else call.column}
        order = {1: (1,), 2: (2,), 3: (3,)}.get(document.position_encoding, (1, 2, 3) if profile.name in ("go", "rust") else (2, 3, 1))
        occurrence = next((at[(call.line - 1, in_units[unit])] for unit in order if (call.line - 1, in_units[unit]) in at), None)
        if occurrence is None:
            continue  # SCIP resolved nothing here
        scope = path if occurrence.symbol.startswith("local ") else ""
        truth = sites.get((scope, occurrence.symbol))
        if truth is None:   # no definition in this index: a library, a builtin, or something SCIP could not resolve
            if answer is None or package_of(occurrence.symbol) in grade.packages:
                continue    # unresolved, or declared by a package of this tree in a way the index does not show
            grade.outside += 1
            given = answer(path, call)
            grade.outside_answered += bool(given)
            grade.outside_certain += bool(given) and given[0].certain
            if len(given) == 1:
                grade.outside_confident += 1
                if len(grade.outside_examples) < grade.keep:
                    grade.outside_examples.append(f"{path}:{call.line} {_written(call)} -> {given[0].path}:{given[0].start_line} "
                                                  f"[{given[0].how}] but SCIP says {occurrence.symbol[:70]}")
            continue
        if truth.name and call.name not in truth.names():
            grade.excluded_untrustworthy += 1  # the oracle names something other than what is written
            continue
        grade.graded += 1
        candidates = answer(path, call) if answer is not None else []
        if not candidates:
            if len(grade.unanswered) < grade.keep:
                grade.unanswered.append(f"{path}:{call.line} {_written(call)} "
                                        f"[{call.via or 'plain'}, {call.head or 'unbound'}] SCIP says {truth.path}:{truth.line + 1}")
            continue
        grade.answered += 1
        right = is_correct(candidates[0], truth)
        grade.correct += right
        confident = len(candidates) == 1
        if candidates[0].certain:
            grade.certain_answered += 1
            grade.certain_correct += right
        if confident:
            grade.confident_answered += 1
            grade.confident_correct += right
        counts = grade.by_how.setdefault(f"{candidates[0].how or 'unsaid'}, {'confident' if confident else 'several'}", [0, 0])
        counts[0] += 1
        counts[1] += right
        if not right and len(grade.disagreements) < grade.keep and (confident or not grade.confident_only):
            grade.disagreements.append(f"{path}:{call.line} {_written(call)} -> "
                                       f"{candidates[0].path}:{candidates[0].start_line} [{candidates[0].how}, {len(candidates)}] "
                                       f"but SCIP says {truth.path}:{truth.line + 1}")


def grade_corpus(root: Path, index: scip.ScipIndex, language: str,
                 answer: Callable[[str, Call], list[Candidate]] | None = None, keep: int = 40,
                 confident_only: bool = False) -> Grade:
    grade = Grade(language, keep=keep, confident_only=confident_only)
    sites = definition_sites(index)
    grade.packages = {package_of(symbol) for _, symbol in sites} | {""}
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


def aleph_graph(root: Path, features: frozenset[str] = FEATURES) -> Graph:
    """Aleph's graph of a corpus on disk, from the same facts the index would hold per file."""
    facts: dict[str, FileFacts] = {}
    manifests: dict[str, bytes] = {}
    for path in tracked_files(root):
        file = root / path
        if not file.is_file() or file.is_symlink():
            continue
        if path.rsplit("/", 1)[-1] in MANIFESTS:
            manifests[path] = file.read_bytes()
        profile = lang.profile_for_path(path)
        if profile is None:
            continue
        data = file.read_bytes()
        tree = parse.tree(profile, data)
        facts[path] = FileFacts(profile.name, definitions(profile, tree, data), bindings(profile, tree, data),
                                bases(profile, tree, data), fields(profile, tree, data))
    return Graph(facts, manifests, features)


def aleph_answer(graph: Graph) -> Callable[[str, Call], list[Candidate]]:
    def answer(path: str, call: Call) -> list[Candidate]:
        edge = graph.resolve(path, call)
        if edge is None:
            return []
        how = f"{edge.level} {edge.provenance}: {edge.rule}"
        return [Candidate(t.path, t.name, t.start_line, t.end_line, how, edge.certain) for t in edge.targets]
    return answer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path, help="holds <corpus>/ and <corpus>.scip for each corpus")
    parser.add_argument("--corpus", action="append",
                        help=f"one of {', '.join(sorted(CORPORA))}; or NAME:LANGUAGE for another corpus prepared the same way")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--population", action="store_true", help="report the population alone, with no answers")
    parser.add_argument("--disagreements", type=int, default=0, help="print this many wrong answers per corpus")
    parser.add_argument("--confident-only", action="store_true", help="of the wrong answers, print those with one candidate")
    parser.add_argument("--unanswered", type=int, default=0, help="print this many graded sites that got no answer")
    parser.add_argument("--outside", type=int, default=0, help="print this many confident answers at sites SCIP places outside")
    parser.add_argument("--by-how", action="store_true", help="print answers and precision by how they were found")
    parser.add_argument("--without", action="append", choices=sorted(FEATURES), default=[],
                        help="switch one of the resolver's extensions off, to measure what it adds")
    args = parser.parse_args(argv)
    report = {}
    languages = dict(CORPORA)
    for given in args.corpus or ():
        name, _, language = given.partition(":")
        if language:
            languages[name] = language
        elif name not in languages:
            parser.error(f"{name} is not one of the six corpora: say its language as {name}:LANGUAGE")
    for name in [given.partition(":")[0] for given in args.corpus] if args.corpus else sorted(CORPORA):
        root, index_file = args.workdir / name, args.workdir / f"{name}.scip"
        if not root.is_dir() or not index_file.is_file():
            print(f"{name}: not prepared under {args.workdir} (the corpus or its .scip is missing)", file=sys.stderr)
            continue
        index = scip.load_index(index_file)
        answer = None if args.population else aleph_answer(aleph_graph(root, FEATURES - set(args.without)))
        grade = grade_corpus(root, index, languages[name], answer, keep=max(args.disagreements, args.unanswered, args.outside, 40),
                             confident_only=args.confident_only)
        report[name] = {**grade.as_dict(), "documents": len(index.documents), "oxn_confident_recall": BARS.get(languages[name])}
        row = report[name]
        print(f"{name:22} graded {row['graded_call_sites']:5} excl {row['excluded_share']:5.1%} | oxn's protocol: recall {row['recall']:.1%}, "
              f"confident precision {row['confident_precision']:.1%}, confident recall {row['confident_recall']:.1%} | "
              f"certain: precision {row['certain_precision_all_sites']:.1%} (with {row['outside_certain']} wrong at {row['outside_sites']} "
              f"outside calls), recall {row['certain_recall']:.1%} | by name at outside calls: {row['outside_confident'] - row['outside_certain']}",
              flush=True)
        if args.by_how:
            for how, counts in row["by_how"].items():
                print(f"    {how:62} answered {counts['answered']:6}  correct {counts['correct']:6}  {counts['precision']:.1%}")
        for line in grade.disagreements[:args.disagreements]:
            print("   ", line)
        for line in grade.outside_examples[:args.outside]:
            print("    outside:", line)
        for line in grade.unanswered[:args.unanswered]:
            print("    no answer:", line)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
