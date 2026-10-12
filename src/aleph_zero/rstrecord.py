"""A decision record written in reStructuredText, read without rendering it.

Python projects write their records this way. What is wanted of one is little: what it is called,
its status, what it rules, what it applies to, and its prose. So no parser of the markup is brought
in: a title is a line with a line of one repeated mark under it, both starting in the first column,
and what the marks mean is read as the markup itself defines it, by the order in which each kind is
first used. Whatever is not understood is prose.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_MARKS = re.compile(r"^([=\-`:.'\"~^_*+#])\1{2,}\s*$")           # a line of one mark, three or more
_FIELD = re.compile(r"^:([A-Za-z][\w -]*):(?:\s+(.*))?$")          # `:Status: Accepted`
_NUMBER = re.compile(r"^(?:(?:ADR[- ]?)?\d+\s*[.:\-–]\s*)", re.I)  # `0011 - `, `3. `, `ADR 12: ` before a title
_COMMENT = re.compile(r"^\.\.(?:\s*$|\s+(?![\w.-]+::|_|\[|\|))")   # `..` that is no directive, target, footnote or substitution
_TARGET = re.compile(r"^\.\. _[^:]*:\s*\S*\s*$")
_ITEM = re.compile(r"^(?:[*+-]|#\.|\d+[.)])\s+(.*)$")
_DECISION = re.compile(r"^decisions?(?:\s+outcome)?(?:\s*(?:&|and)\s*consequences)?(?:\s*:.*)?$", re.I)
# What a record's parts are called. A record that opens with one has no title of its own; and a Decision ends at
# one of these however it is marked, since writers mark "Consequences" as they like.
_PARTS = re.compile(r"^(?:status|context(?: and problem statement)?|problem(?: statement)?|background|summary|abstract|rationale|decisions?"
                    r"(?: outcome)?(?:\s*(?:&|and)\s*consequences)?|consequences|references|changelog|change ?log|further (?:reading|discussion)"
                    r"|(?:rejected |considered )?alternatives(?: considered| rejected)?|pros and cons(?: of the options)?|notes)\s*:?$", re.I)
_FILING = frozenset({"status", "date", "applies-to", "tags", "id", "author", "authors", "deciders", "decision-makers", "consulted",
                     "informed", "version", "revision", "updated", "created", "supersedes", "superseded-by", "orphan"})
_STATUS_LINE = re.compile(r"^\**Status\**\s*:\s*(.+?)\s*$", re.M | re.I)


@dataclass
class Read:
    title: str = ""
    status: str = ""
    decision: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    body: str = ""


def _prose(lines: list[str]) -> list[str]:
    """The lines without comments (with what is indented under them) and without link targets."""
    kept, at = [], 0
    while at < len(lines):
        line = lines[at]
        if _TARGET.match(line):
            at += 1
        elif _COMMENT.match(line):
            at += 1
            while at < len(lines) and (not lines[at].strip() or lines[at][:1] in " \t"):
                at += 1
        else:
            kept.append(line)
            at += 1
    return kept


def _headings(lines: list[str]) -> list[tuple[int, int, str, tuple[str, bool]]]:
    """(first line, line after its marks, text, kind) of every title. A kind is its mark and whether a line of it
    stands above too. Only what starts in the first column: what is indented is a quotation or a literal block."""
    found = []
    for at in range(len(lines) - 1):
        text, under = lines[at], _MARKS.match(lines[at + 1])
        if under is None or not text.strip() or _MARKS.match(text) or _FIELD.match(text):
            continue
        over = at > 0 and lines[at - 1].startswith(under.group(1) * 3) and _MARKS.match(lines[at - 1]) is not None
        if text[:1] in " \t" and not over:
            continue
        if len(lines[at + 1].rstrip()) < min(len(text.strip()), 4):
            continue
        found.append((at - 1 if over else at, at + 2, text.strip(), (under.group(1), over)))
    return found


def _plain(text: str) -> str:
    return re.sub(r"\*+|`+", "", text).strip()


def read(content: str) -> Read:
    lines = _prose(content.replace("\r\n", "\n").split("\n"))
    headings = _headings(lines)
    out = Read()
    # Fields before the first section (above the title, or right under it) describe the record: they are not prose.
    first_section = headings[1][0] if len(headings) > 1 else len(lines)
    dropped: set[int] = set()
    at = 0
    while at < first_section:
        named = _FIELD.match(lines[at])
        if named is not None:
            value, dropped_here = [named.group(2) or ""], {at}
            while at + 1 < first_section and lines[at + 1][:1] in " \t" and lines[at + 1].strip():      # a value that goes on
                at += 1
                value.append(lines[at].strip())
                dropped_here.add(at)
            name = named.group(1).strip().lower()
            out.fields.setdefault(name, " ".join(part for part in value if part).strip())
            if name in _FILING:         # how the record is filed is not prose; a field that says what it is about is
                dropped |= dropped_here
        at += 1
    if headings and not _PARTS.match(headings[0][2]):      # a record that opens with "Status" has no title of its own
        out.title = _NUMBER.sub("", headings[0][2]).strip() or headings[0][2]
    order: list[tuple[str, bool]] = []
    for _, _, _, kind in headings:
        if kind not in order:
            order.append(kind)
    for place, (_, start, text, kind) in enumerate(headings):
        name = text.strip().rstrip(":").strip()
        if name.lower() == "status" and not out.status:
            end = headings[place + 1][0] if place + 1 < len(headings) else len(lines)
            block: list[str] = []
            for line in lines[start:end]:
                if line.strip():
                    block.append(line.strip())
                elif block:
                    break
            items = [_ITEM.match(line) for line in block]
            if block:       # a history is a list, and its last entry is where the record stands
                out.status = _plain(items[-1].group(1) if all(items) and len(items) > 1 else (items[0].group(1) if items[0] else block[0]))
        elif _DECISION.match(name) and not out.decision:
            # To the next title of its own kind or of one used before it: a kind first used later is a part of it.
            seen = order[:order.index(kind) + 1]
            end = next((first for first, _, other_text, other in headings[place + 1:] if other in seen or _PARTS.match(other_text)), len(lines))
            out.decision = "\n".join(lines[start:end]).strip("\n") + "\n" if any(line.strip() for line in lines[start:end]) else ""
    if not out.status:
        line = _STATUS_LINE.search("\n".join(lines))
        out.status = _plain(out.fields.get("status", "") or (line.group(1) if line else ""))
    out.body = "\n".join(line for at, line in enumerate(lines) if at not in dropped)
    return out
