"""Which decision governs a change: decision records ranked as whole documents.

Search ranks chunks, and for a long question keeps only its rarest words: right for finding
where something is, wrong here. A decision record is one document, and the question is a
task or a commit message: many ordinary words, each a little evidence. So the records of a
view are ranked whole, by BM25 over title and text with every word of the task, identifiers
split into their parts (a task says "the ADR loader" where the record says `load_decisions`).
Where the worktree has vectors, the rank of each record's nearest chunk is fused with that.

A record is a numbered Markdown file in a directory named for decisions (`docs/adr/0012-…`),
or one that calls itself an ADR wherever it lies (`docs/adr-004-…`), in the form adr-tools
writes or with front matter. The ranking
follows oxn's (`context/bm25.py`, `rules/adr.py`, v0.1.2, MIT, same author), which is also
what it is measured against (ADR-007).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

DIRECTORIES = frozenset({"adr", "adrs", "decisions", "decision-records", "decision_records", "architecture-decisions"})
MAX_BYTES = 400_000
K1, B = 1.5, 0.75
RRF_K = 5               # small: with tens of records, first and second place must differ by more than 1/61 and 1/62
NEAREST = 200           # chunks of the records asked of the vectors
_NUMBERED = re.compile(r"(?:ADR[-_ ]?)?(\d+)[-_]", re.I)
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
_HEADING = re.compile(r"^#\s+(?:(?:ADR[- ]?)?\d+\s*[.:\-–]\s*)?(.+?)\s*$", re.MULTILINE | re.I)
_STATUS_SECTION = re.compile(r"^##\s*Status\s*$\n+(.+?)\s*$", re.MULTILINE | re.I)
_STATUS_LINE = re.compile(r"^\**Status\**\s*:\s*\**\s*(.+?)\s*\**\s*$", re.MULTILINE | re.I)
_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_FIELD = re.compile(r"^([A-Za-z_-]+)\s*:\s*(.*?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Decision:
    path: str
    number: int
    title: str
    status: str
    text: str           # what is ranked: title, tags, and the record as written


@dataclass(frozen=True)
class DecisionHit:
    path: str
    number: int
    title: str
    status: str
    score: float
    sources: tuple[str, ...]    # 'words', 'vectors': what put it where it is


@dataclass(frozen=True)
class Answer:
    task: str
    decisions: list[DecisionHit]
    known: int = 0              # decision records in the view
    vectors: str = ""           # "used", or why vectors took no part; "" when they are switched off
    elapsed_ms: int = 0


def is_record(path: str) -> int | None:
    """The number of the decision a path holds, or None when it is not a decision record."""
    parts = path.split("/")
    numbered = _NUMBERED.match(parts[-1])
    if numbered is None or not parts[-1].lower().endswith((".md", ".mdx")):
        return None
    # In a directory of decisions any numbered file is one; elsewhere only a file that calls itself an ADR.
    named = parts[-1].lower().startswith("adr")
    return int(numbered.group(1)) if named or any(part.lower() in DIRECTORIES for part in parts[:-1]) else None


def parse(path: str, number: int, content: str) -> Decision:
    front = _FRONT.match(content)
    fields = {key.lower(): value.strip("\"'") for key, value in _FIELD.findall(front.group(1))} if front else {}
    body = content[front.end():] if front else content
    heading = _HEADING.search(body)
    title = fields.get("title") or (heading.group(1) if heading else path.rsplit("/", 1)[-1])
    section, line = _STATUS_SECTION.search(body), _STATUS_LINE.search(body)
    status = fields.get("status") or (section.group(1) if section else line.group(1) if line else "")
    if fields.get("id") and re.search(r"\d", fields["id"]):
        number = int(re.sub(r"\D", "", fields["id"]))
    tags = fields.get("tags", "").strip("[]").replace(",", " ")
    return Decision(path, number, title.strip(), status.strip(), f"{title}\n{tags}\n{body}")


def tokenize(text: str) -> list[str]:
    """Lowercased tokens, identifiers split into their parts; acronyms kept whole."""
    return [match.group(0).lower() for match in _TOKEN.finditer(text)]


class Records:
    """The decision records of one view, ready to be ranked."""

    def __init__(self, found: list[Decision]) -> None:
        self.decisions = sorted(found, key=lambda each: (each.number, each.path))
        counted = [Counter(tokenize(each.text)) for each in self.decisions]
        self._lengths = [sum(terms.values()) for terms in counted]
        self._average = sum(self._lengths) / len(self._lengths) if self._lengths else 0.0
        self._postings: dict[str, dict[int, int]] = {}
        for at, terms in enumerate(counted):
            for term, frequency in terms.items():
                self._postings.setdefault(term, {})[at] = frequency

    def by_words(self, task: str) -> list[tuple[int, float]]:
        """(position in `decisions`, BM25 score) of the records that share a word with the task, best first.
        Equal scores keep the records' own order, so the ranking is a function of the records and the task."""
        scores: dict[int, float] = {}
        size = len(self.decisions)
        for term in tokenize(task):
            found = self._postings.get(term)
            if found is None:
                continue
            weight = math.log(1 + (size - len(found) + 0.5) / (len(found) + 0.5))
            for at, frequency in found.items():
                norm = K1 * (1 - B + B * (self._lengths[at] / self._average if self._average else 1.0))
                scores[at] = scores.get(at, 0.0) + weight * frequency * (K1 + 1) / (frequency + norm)
        return sorted(scores.items(), key=lambda hit: (-hit[1], hit[0]))

    def rank(self, task: str, by_vectors: list[str] | None = None, k: int = 7, rrf_k: int = RRF_K) -> list[DecisionHit]:
        """The `k` records for a task. `by_vectors`: the records' paths in the order of their nearest chunk,
        when the worktree has vectors; the two orders are fused by reciprocal rank."""
        if not task.strip():
            return [DecisionHit(each.path, each.number, each.title, each.status, 0.0, ()) for each in self.decisions[:k]]
        words = self.by_words(task)
        if not by_vectors:
            return [self._hit(at, score, ("words",)) for at, score in words[:k]]
        place = {each.path: at for at, each in enumerate(self.decisions)}
        fused: dict[int, float] = {}
        sources: dict[int, tuple[str, ...]] = {}
        for rank, (at, _) in enumerate(words, 1):
            fused[at] = fused.get(at, 0.0) + 1 / (rrf_k + rank)
            sources[at] = ("words",)
        for rank, path in enumerate(by_vectors, 1):
            at = place.get(path)
            if at is not None:
                fused[at] = fused.get(at, 0.0) + 1 / (rrf_k + rank)
                sources[at] = (*sources.get(at, ()), "vectors")
        ordered = sorted(fused.items(), key=lambda hit: (-hit[1], hit[0]))
        return [self._hit(at, score, sources[at]) for at, score in ordered[:k]]

    def _hit(self, at: int, score: float, sources: tuple[str, ...]) -> DecisionHit:
        each = self.decisions[at]
        return DecisionHit(each.path, each.number, each.title, each.status, round(score, 4), sources)


def text(answer: Answer) -> str:
    """The answer for a reader: one line per decision. Path, number, title, status."""
    if not answer.known:
        return ("no decision records in this worktree (numbered Markdown files in a directory such as docs/adr: "
                "`0012-use-an-event-bus.md`)")
    if not answer.decisions:
        return f"none of the {answer.known} decision records shares a word with the task"
    lines = [f"{hit.path}  ADR {hit.number}  {hit.title}" + (f"  [{hit.status}]" if hit.status else "") for hit in answer.decisions]
    if answer.vectors not in ("", "used"):
        lines.append(f"note: ranked by words alone ({answer.vectors})")
    return "\n".join(lines)
