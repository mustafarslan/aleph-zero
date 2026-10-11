"""Chunkers that need no grammar: Markdown by heading, everything else by line window."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from .model import Chunk

WINDOW_LINES = 40
WINDOW_STRIDE = 30
SECTION_LINES = 60
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def window_chunks(text: str, kind: str = "block", breadcrumb: str = "", first_line: int = 1,
                  size: int = WINDOW_LINES, stride: int = WINDOW_STRIDE) -> list[Chunk]:
    """Overlapping windows of lines. Every non-blank line lands in at least one chunk."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        window = lines[start:start + size]
        if any(line.strip() for line in window):
            symbol = breadcrumb.rsplit(" > ", 1)[-1]
            chunks.append(Chunk(kind, symbol, "", breadcrumb, "", "\n".join(window),
                                first_line + start, first_line + start + len(window) - 1))
        if start + size >= len(lines):
            break
        start += stride
    return chunks


def markdown_chunks(text: str) -> list[Chunk]:
    """One chunk per heading section, with the heading trail as breadcrumb.
    A '#' inside a fenced code block is not a heading."""
    lines = text.replace("\r\n", "\n").split("\n")
    sections: list[tuple[int, list[str], str]] = []  # (first line index, trail, heading line)
    trail: list[tuple[int, str]] = []
    in_fence = False
    start, heading = 0, ""
    current_trail: list[str] = []

    def close(end: int) -> None:
        body = lines[start:end]
        if any(line.strip() for line in body):
            sections.append((start, list(current_trail), heading))

    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            close(index)
            level, title = len(match.group(1)), match.group(2)
            trail = [(lvl, name) for lvl, name in trail if lvl < level] + [(level, title)]
            current_trail = [name for _, name in trail]
            start, heading = index, line.strip()
    close(len(lines))

    chunks: list[Chunk] = []
    ends = [s[0] for s in sections[1:]] + [len(lines)]
    for (first, names, heading_line), end in zip(sections, ends):
        body = lines[first:end]
        while body and not body[-1].strip():
            body.pop()
        breadcrumb = " > ".join(names)
        symbol = names[-1] if names else ""
        if len(body) <= SECTION_LINES:
            chunks.append(Chunk("doc", symbol, heading_line, breadcrumb, "", "\n".join(body),
                                first + 1, first + len(body)))
            continue
        for piece in window_chunks("\n".join(body), "doc", breadcrumb, first + 1, WINDOW_LINES, WINDOW_LINES):
            chunks.append(Chunk("doc", symbol, heading_line, breadcrumb, "", piece.text,
                                piece.start_line, piece.end_line))
    return chunks


def chunk_file(path: str, kind: str, text: str) -> list[Chunk]:
    """Chunks for a file with no language-specific chunker (those arrive with the language layer)."""
    if PurePosixPath(path).suffix.lower() in {".md", ".mdx"}:
        return markdown_chunks(text)
    return window_chunks(text, kind="doc" if kind == "doc" else "block")
