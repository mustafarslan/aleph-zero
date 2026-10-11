"""Chunks that follow the code's own structure.

Every function and method is a chunk. A class is a chunk of whatever its methods do
not cover: its header, docstring and fields. What is left of the file (imports,
constants, top-level statements) is chunked as the module. Anything longer than
SPLIT_TOKENS is cut into pieces at line boundaries; the pieces share the definition's
name, signature and line range, so search can report them as one result.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..lang import LanguageProfile, parse
from ..lang.definitions import Definition, definitions
from ..lang.imports import RawImport, extract_imports
from .fallback import window_chunks
from .model import TOKENS_PER_CHAR, Chunk

SPLIT_TOKENS = 256  # ADR-001: provisional until the M2 sweep
_CALLABLE = frozenset({"function", "method"})
_CONTAINER = frozenset({"class", "interface", "enum", "type", "namespace"})
_MODULE = -1
# A piece smaller than this is not closed at a gap in its lines: small fragments
# between methods stay with their neighbours instead of becoming chunks of their own.
_GLUE_TOKENS = 64

Runs = tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class CodeFile:
    chunks: list[Chunk]
    runs: list[Runs]                 # for each chunk, the runs of source lines it holds
    definitions: list[Definition]
    chunk_of: list[int]              # for each definition, the chunk holding its first line
    imports: list[RawImport]


def code_chunks(profile: LanguageProfile, text: str) -> CodeFile:
    """Chunks, definitions and imports of one source file. A file the parser cannot
    handle at all is chunked by line window."""
    text = text.replace("\r\n", "\n")
    data = text.encode("utf-8", "surrogateescape")
    try:
        tree = parse.tree(profile, data)
        found = definitions(profile, tree, data)
        imported = extract_imports(tree.root_node, profile)
    except Exception:  # noqa: BLE001 - a grammar that fails to load or a parser that gives up
        windows = window_chunks(text)
        return CodeFile(windows, [((c.start_line, c.end_line),) for c in windows], [], [], [])

    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    owner = _owners(found, len(lines))
    chunks: list[tuple[Chunk, Runs]] = []
    for unit in sorted(set(owner)):
        numbers = [number for number, who in enumerate(owner, 1) if who == unit] if unit == _MODULE else None
        chunks.extend(_unit_chunks(unit, found, owner, lines, numbers))
    chunks.sort(key=lambda item: (item[0].start_line, item[0].end_line))
    line_chunk = [0] * (len(lines) + 2)
    for ordinal, (_, runs) in enumerate(chunks):
        for first, last in runs:
            for number in range(first, last + 1):
                line_chunk[number] = ordinal
    chunk_of = [line_chunk[min(max(d.start_line, 1), len(lines))] if lines else 0 for d in found]
    return CodeFile([chunk for chunk, _ in chunks], [runs for _, runs in chunks], found, chunk_of, imported)


def _owners(found: list[Definition], count: int) -> list[int]:
    """For each line (index 0 is line 1), the definition that owns it, or _MODULE.
    Callables claim their lines first; a container owns what is left inside it, the
    innermost one winning."""
    owner = [_MODULE] * count
    claimed = [False] * count
    for index, definition in enumerate(found):
        if definition.kind in _CALLABLE:
            for number in range(definition.start_line - 1, min(definition.end_line, count)):
                if not claimed[number]:
                    owner[number] = index
                    claimed[number] = True
    for index, definition in enumerate(found):  # source order: an inner container comes later
        if definition.kind in _CONTAINER:
            for number in range(definition.start_line - 1, min(definition.end_line, count)):
                if not claimed[number]:
                    owner[number] = index
    return owner


def _unit_chunks(unit: int, found: list[Definition], owner: list[int], lines: list[str],
                 numbers: list[int] | None) -> list[tuple[Chunk, Runs]]:
    if unit == _MODULE:
        kind, symbol, signature, breadcrumb, doc, scope = "module", "", "", "", "", (0, 0)
    else:
        definition = found[unit]
        kind, symbol, signature = definition.kind, definition.name, definition.signature
        breadcrumb, doc = definition.qualified, definition.doc
        scope = (definition.start_line, min(definition.end_line, len(lines)))
        numbers = [number for number in range(scope[0], scope[1] + 1) if owner[number - 1] == unit]
    out: list[tuple[Chunk, Runs]] = []
    for piece in _pieces(numbers, lines):
        body = "\n".join(lines[number - 1] for number in piece)
        if not any(character.isalnum() for character in body):
            continue  # closing braces and blank lines say nothing
        whole = scope if (piece[0], piece[-1]) != scope else (0, 0)
        out.append((Chunk(kind, symbol, signature, breadcrumb, doc, body, piece[0], piece[-1], *whole), _runs(piece)))
    return out


def _pieces(numbers: list[int], lines: list[str]) -> list[list[int]]:
    """Cut a unit's lines into pieces of at most SPLIT_TOKENS, estimated from length.
    A piece ends early at a gap in the line numbers once it is big enough, and a cut
    forced by size falls back to the last blank line of the piece when there is one.
    One line over the limit is a piece by itself: nothing is ever truncated."""
    limit = SPLIT_TOKENS / TOKENS_PER_CHAR
    glue = _GLUE_TOKENS / TOKENS_PER_CHAR
    pieces: list[list[int]] = []
    current: list[int] = []
    size = 0.0
    for number in numbers:
        cost = len(lines[number - 1]) + 1
        gap = bool(current) and number != current[-1] + 1
        if current and (size + cost > limit or (gap and size >= glue)):
            if not gap and size + cost > limit:
                current, carried = _cut_at_blank(current, lines)
            else:
                carried = []
            pieces.append(current)
            current = carried
            size = float(sum(len(lines[n - 1]) + 1 for n in current))
        current.append(number)
        size += cost
    if current:
        pieces.append(current)
    return [_trim(piece, lines) for piece in pieces if _trim(piece, lines)]


def _cut_at_blank(piece: list[int], lines: list[str]) -> tuple[list[int], list[int]]:
    """Split a full piece at its last blank line, if that leaves at least half of it."""
    for position in range(len(piece) - 1, len(piece) // 2, -1):
        if not lines[piece[position] - 1].strip():
            return piece[:position], piece[position + 1:]
    return piece, []


def _trim(piece: list[int], lines: list[str]) -> list[int]:
    """Without the blank lines at either end."""
    first, last = 0, len(piece)
    while first < last and not lines[piece[first] - 1].strip():
        first += 1
    while last > first and not lines[piece[last - 1] - 1].strip():
        last -= 1
    return piece[first:last]


def _runs(piece: list[int]) -> Runs:
    runs: list[tuple[int, int]] = []
    start = previous = piece[0]
    for number in piece[1:]:
        if number != previous + 1:
            runs.append((start, previous))
            start = number
        previous = number
    runs.append((start, previous))
    return tuple(runs)
