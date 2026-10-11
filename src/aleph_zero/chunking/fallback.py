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
_SETEXT = re.compile(r"^ {0,3}(={2,}|-{2,})\s*$")
_ADORNMENT = re.compile(r"^([=\-`:'\"~^_*+#<>.])\1{2,}\s*$")


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


Heading = tuple[int, int, str]  # (index of its first line, level, title)


def markdown_chunks(text: str) -> list[Chunk]:
    """One chunk per heading section, with the heading trail as breadcrumb. Headings are
    `# ATX` lines and setext titles underlined with `===` or `---`. A '#' inside a
    fenced code block is not a heading, nor is anything in YAML front matter."""
    lines = text.replace("\r\n", "\n").split("\n")
    headings: list[Heading] = []
    in_fence = False
    start = 0
    if lines and lines[0].strip() == "---":  # front matter: skip to its closing line
        closing = next((i for i in range(1, len(lines)) if lines[i].strip() in ("---", "...")), 0)
        start = closing + 1 if closing else 0
    for index in range(start, len(lines)):
        line = lines[index]
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _HEADING.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))
            continue
        underline = _SETEXT.match(line)
        if underline and index > start and _is_paragraph_line(lines[index - 1]) and (
                not headings or headings[-1][0] != index - 1):
            headings.append((index - 1, 1 if underline.group(1)[0] == "=" else 2, lines[index - 1].strip()))
    return _sectioned(lines, headings)


def rst_chunks(text: str) -> list[Chunk]:
    """reStructuredText by section. A title is a line of text underlined (and optionally
    overlined) by one repeated punctuation character at least as long as the title. Its
    level is not written anywhere: it is the order in which adornment styles first appear."""
    lines = text.replace("\r\n", "\n").split("\n")
    headings: list[Heading] = []
    styles: list[tuple[str, bool]] = []
    index = 0
    while index < len(lines) - 1:
        title, under = lines[index], lines[index + 1]
        adornment = _ADORNMENT.match(under)
        if (adornment and title.strip() and not title[:1].isspace() and not _ADORNMENT.match(title)
                and len(under.rstrip()) >= len(title.rstrip())):
            overlined = index > 0 and lines[index - 1].rstrip() == under.rstrip()
            style = (under[0], overlined)
            if style not in styles:
                styles.append(style)
            headings.append((index - 1 if overlined else index, styles.index(style) + 1, title.strip()))
            index += 2
        else:
            index += 1
    return _sectioned(lines, headings)


def _is_paragraph_line(line: str) -> bool:
    """A line a setext underline can turn into a title: text, not a list item, quote,
    table row, indented code or another underline."""
    stripped = line.strip()
    return bool(stripped) and not line.startswith(("    ", "\t")) and not stripped.startswith(
        ("#", ">", "|", "- ", "* ", "+ ", "```", "~~~")) and "|" not in stripped and not _SETEXT.match(line)


def _sectioned(lines: list[str], headings: list[Heading]) -> list[Chunk]:
    """Chunks for the text before the first heading and for each heading's section."""
    starts = [0] + [index for index, _, _ in headings]
    ends = starts[1:] + [len(lines)]
    trail: list[tuple[int, str]] = []
    chunks: list[Chunk] = []
    for position, (first, end) in enumerate(zip(starts, ends)):
        names: list[str] = []
        heading_line = ""
        if position > 0:
            _, level, title = headings[position - 1]
            trail = [(lvl, name) for lvl, name in trail if lvl < level] + [(level, title)]
            names = [name for _, name in trail]
            heading_line = title
        body = lines[first:end]
        while body and not body[-1].strip():
            body.pop()
        if not any(line.strip() for line in body):
            continue
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


def chunk_file(family: str, kind: str, text: str) -> list[Chunk]:
    """Chunks for a family with no language-specific chunker."""
    if family == "markdown":
        return markdown_chunks(text)
    if family == "prose":
        return rst_chunks(text)  # plain text has no titles and comes out as windows
    return window_chunks(text, kind="doc" if kind == "doc" else "block")
