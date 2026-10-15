"""A file's skeleton: what it defines and where, instead of reading the whole file.

The outline is computed from the file as the view sees it right now (the working copy
if it differs from HEAD), so it needs no index and cannot be stale.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import filters, gitio, lang
from .chunking import estimate_tokens, markdown_chunks, rst_chunks
from .lang import parse
from .lang.definitions import definitions
from .view import View

DEFAULT_BUDGET = 1500


class OutlineError(ValueError):
    """The path cannot be outlined: it is not in the view, or is not text."""


@dataclass(frozen=True)
class Item:
    depth: int
    kind: str
    name: str
    qualified: str
    signature: str
    start_line: int
    end_line: int
    nested: bool = False   # defined inside a function: shown under it, and the first to go when it does not fit


@dataclass(frozen=True)
class Outline:
    path: str
    family: str
    lines: int
    items: list[Item] = field(default_factory=list)


def of(view: View, path: str) -> Outline:
    """The outline of one file of the view."""
    if path not in view.files:
        raise OutlineError(f"{path} is not a file in this worktree")
    family = filters.family(path)
    with gitio.Readers() as reader:
        data = view.read(path, reader, filters.MAX_BYTES)
    if data is None:
        raise OutlineError(f"{path} could not be read")
    if isinstance(data, gitio.TooLarge):
        raise OutlineError(f"{path} is too large to outline ({data.size} bytes)")
    kind, text = filters.classify(family, data)
    if text is None:
        raise OutlineError(f"{path} is not outlined: {kind.removeprefix('skip:')}")
    text = text.replace("\r\n", "\n")
    count = text.count("\n") + (0 if text.endswith("\n") else 1)
    profile = lang.PROFILES.get(family)
    if profile is not None:
        return Outline(path, family, count, _code_items(profile, text))
    if family in ("markdown", "prose"):
        return Outline(path, family, count, _doc_items(family, text))
    return Outline(path, family, count)


def _code_items(profile, text: str) -> list[Item]:
    data = text.encode("utf-8", "surrogateescape")
    try:
        found = definitions(profile, parse.tree(profile, data), data, inner=True)
    except Exception:  # noqa: BLE001 - a grammar that fails to load: no structure, not an error
        return []
    depths: list[int] = []
    items: list[Item] = []
    for definition in found:
        depth = 0 if definition.parent is None else depths[definition.parent] + 1
        depths.append(depth)
        items.append(Item(depth, definition.kind, definition.name, definition.qualified, definition.signature,
                          definition.start_line, definition.end_line, definition.nested))
    return items


def _doc_items(family: str, text: str) -> list[Item]:
    chunks = markdown_chunks(text) if family == "markdown" else rst_chunks(text)
    items: list[Item] = []
    for chunk in chunks:
        if not chunk.breadcrumb:
            continue  # text before the first heading
        if items and items[-1].qualified == chunk.breadcrumb and not _opens_section(chunk):
            # a long section came out as several chunks
            last = items[-1]
            items[-1] = Item(last.depth, last.kind, last.name, last.qualified, last.signature,
                             last.start_line, chunk.end_line)
            continue
        items.append(Item(chunk.breadcrumb.count(" > "), "section", chunk.symbol, chunk.breadcrumb, chunk.symbol,
                          chunk.start_line, chunk.end_line))
    return items


def _opens_section(chunk) -> bool:
    """True if the chunk starts with its section's heading, so it is a section of its own
    and not the continuation of the one before, even when both have the same title."""
    opening = [line.strip().strip("#").strip() for line in chunk.text.split("\n")[:2]]
    return chunk.symbol in opening


def text(outline: Outline, budget_tokens: int = DEFAULT_BUDGET) -> str:
    """The outline as indented lines, inside the budget. When it does not fit, what is defined inside functions goes
    first (on a file of callbacks it would crowd out the file's own structure), then nested definitions give way to
    top-level ones, and what is left out is counted."""
    head = f"{outline.path}  {outline.family}  {outline.lines} lines"
    if not outline.items:
        return head + "\n  (no structure found)"
    whole = _lines(head, outline.items, 0, budget_tokens)
    if whole is not None:
        return whole
    own = [item for item in outline.items if not item.nested]
    if not own:
        own = outline.items
    deepest = max(item.depth for item in own)
    for max_depth in range(deepest, -1, -1):
        kept = [item for item in own if item.depth <= max_depth]
        rendered = _lines(head, kept, len(outline.items) - len(kept), budget_tokens)
        if rendered is not None:
            return rendered
    top = [item for item in outline.items if item.depth == 0]
    return _lines(head, top, len(outline.items) - len(top), budget_tokens, cut=True) or head


def _lines(head: str, items: list[Item], hidden: int, budget: int, cut: bool = False) -> str | None:
    out = [head]
    spent = estimate_tokens(head) + 1
    for position, item in enumerate(items):
        span = str(item.start_line) if item.start_line == item.end_line else f"{item.start_line}-{item.end_line}"
        line = f"{'  ' * (item.depth + 1)}{span}  {item.kind}  {item.signature or item.name}"
        left = hidden + len(items) - position - 1
        note = f"  (+{left} more definitions not shown)" if left else ""
        if spent + estimate_tokens(line) + 1 + (estimate_tokens(note) + 1 if note else 0) > budget:
            if not cut:
                return None
            out.append(f"  (+{hidden + len(items) - position} more definitions not shown)")
            return "\n".join(out)
        out.append(line)
        spent += estimate_tokens(line) + 1
    if hidden:
        out.append(f"  (+{hidden} more definitions not shown)")
    return "\n".join(out)
