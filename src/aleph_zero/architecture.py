"""What a repository says about its own architecture in an `oxn.yaml`: layers (a name for a
set of paths) and contracts (which layer may depend on which).

The file is oxn's (ADR-007); Aleph reads it when it is there and enforces nothing. Only the two
sections are read, in the forms oxn's own file and documentation write them: a flow list
(`[a, "b"]`, over one line or several) or a block list (`- a`). There is no YAML parser among
Aleph's dependencies and one optional file does not earn one: a form this does not know
(anchors, flow mappings) states nothing here. Paths match as oxn matches them, with `fnmatch`,
whose `*` crosses directories.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

FILE = "oxn.yaml"           # at the root of the worktree
MAX_BYTES = 262_144


@dataclass(frozen=True)
class Layer:
    name: str
    patterns: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class Contract:
    name: str
    kind: str
    order: tuple[str, ...]      # of a layered contract, outermost first: a layer may depend on those after it
    line: int


def covers(patterns, path: str) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def read(text: str) -> tuple[list[Layer], list[Contract]]:
    """The layers and contracts a file declares, in its order; nothing for what cannot be read."""
    layers, contracts, _ = _parse(text)
    return layers, contracts


def unread(text: str) -> int:
    """How many lines of the two sections said something this reader did not understand: a file half read
    must not look like a file that says less."""
    return _parse(text)[2]


def _parse(text: str) -> tuple[list[Layer], list[Contract], int]:
    layers: list[list] = []
    contracts: list[dict] = []
    section, open_list, item_indent, layer_indent, skipped = "", None, 0, None, 0
    for indent, line, number in _entries(text):
        if indent == 0 and not line.startswith("- "):
            section, open_list, layer_indent = (line[:-1] if line in ("layers:", "contracts:") else ""), None, None
            continue
        if section == "layers":
            if line.startswith("- "):
                if open_list is not None:
                    open_list.append(_scalar(line[2:]))
                else:
                    skipped += 1
                continue
            name, colon, value = line.partition(":")
            layer_indent = indent if layer_indent is None else layer_indent
            listed = not value.strip() or value.strip().startswith("[")
            if colon and _plain(name) and listed and indent == layer_indent:
                open_list = _flow(value) if value.strip() else []
                layers.append([_scalar(name), open_list, number])
            else:       # a mapping under a layer, a value that is no list: not a layer of that name
                open_list = None
                skipped += 1
        elif section == "contracts":
            if line.startswith("- ") and (not contracts or indent <= item_indent or open_list is None):
                contracts.append({"line": number})
                item_indent, open_list, line = indent, None, line[2:].strip()
            elif line.startswith("- "):
                open_list.append(_scalar(line[2:]))
                continue
            name, colon, value = line.partition(":")
            if not colon or not contracts or not _plain(name):
                skipped += 1
                continue
            open_list = None
            if not value.strip():
                open_list = contracts[-1][name.strip()] = []
            elif value.strip().startswith("["):
                contracts[-1][name.strip()] = _flow(value)
            else:
                contracts[-1][name.strip()] = _scalar(value)
    named = [found for found in contracts if isinstance(found.get("name"), str) and found["name"]]
    skipped += sum(1 for _, patterns, _ in layers if not patterns) + len(contracts) - len(named)
    return ([Layer(name, tuple(patterns), line) for name, patterns, line in layers if patterns],
            [Contract(found["name"], found.get("kind", "") if isinstance(found.get("kind", ""), str) else "",
                      tuple(found["order"]) if isinstance(found.get("order"), list) else (), found["line"]) for found in named],
            skipped)


def sequence(text: str, key: str) -> tuple[str, ...]:
    """The list a top-level key holds, written as a flow list or a block list."""
    found: list[str] | None = None
    for indent, line, _ in _entries(text):
        if found is not None and line.startswith("- "):
            found.append(_scalar(line[2:]))
        elif found is not None:
            break
        elif indent == 0 and line.partition(":")[0].strip() == key:
            value = line.partition(":")[2].strip()
            if value.startswith("["):
                return tuple(_flow(value))
            if value:
                return (_scalar(value),)
            found = []
    return tuple(found or ())


def place(path: str, layers: list[Layer], contracts: list[Contract]) -> list[str]:
    """Where a path sits: its layer, and per layered contract what that layer may depend on and what not.
    A path that several layers name is in the first of them, as oxn has it."""
    if not layers:
        return []
    layer = next((each for each in layers if covers(each.patterns, path)), None)
    if layer is None:
        return [f"in no layer of {FILE}"]
    out = []
    for contract in contracts:
        if layer.name in contract.order:
            at = contract.order.index(layer.name)
            inner, outer = contract.order[at + 1:], contract.order[:at]
            out.append(f"layer {layer.name} (contract {contract.name}): may depend on {', '.join(inner) or 'no other layer'}"
                       + (f"; not on {', '.join(outer)}" if outer else ""))
    return out or [f"layer {layer.name}"]


def _entries(text: str) -> list[tuple[int, str, int]]:
    """(indent, content, line) of each line that says something, a flow list over several lines made one."""
    out: list[tuple[int, str, int]] = []
    opened: list | None = None
    for number, raw in enumerate(text.split("\n"), 1):
        line = _uncommented(raw).rstrip()
        if opened is not None:
            opened[1] += " " + line.strip()
            if opened[1].count("[") <= opened[1].count("]"):
                out.append(tuple(opened))
                opened = None
            continue
        if not line.strip() or "\t" in line[:len(line) - len(line.lstrip())]:
            continue
        entry = [len(line) - len(line.lstrip(" ")), line.strip(), number]
        if line.count("[") > line.count("]"):
            opened = entry
        else:
            out.append(tuple(entry))
    return out


def _uncommented(line: str) -> str:
    quote = ""
    for at, char in enumerate(line):
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == "#" and (at == 0 or line[at - 1] in " \t"):
            return line[:at]
    return line


def _scalar(written: str) -> str:
    written = written.strip()
    return written[1:-1] if len(written) >= 2 and written[0] == written[-1] and written[0] in "\"'" else written


def _flow(written: str) -> list[str]:
    written = written.strip()
    if not (written.startswith("[") and written.endswith("]")):
        return []
    items, item, quote = [], "", ""
    for char in written[1:-1]:      # split at commas, but not at one inside quotes: "x/{a,b}/*"
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
        elif char == ",":
            items.append(item)
            item = ""
            continue
        item += char
    return [_scalar(each) for each in (*items, item) if each.strip()]


def _plain(name: str) -> bool:
    name = name.strip()
    return bool(name) and not any(char in name for char in "{}[],")
