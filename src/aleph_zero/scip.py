"""Reading a SCIP index: the compiler-grade oracle the code graph is graded against (ADR-003, ADR-004).

Ported from oxn v0.1.2 (`oxn/scip/wire.py` and `oxn/scip/index.py`; MIT, same author), with
two changes: a fixed-width field that runs past the end of the message is an error here,
where oxn would silently read a short value, and both files are one module.

**Why hand-rolled.** Six fields are needed from a stable, versioned schema. The alternative
is the `protobuf` runtime plus a generated module, for a feature that is optional. Only the
field numbers used are decoded; every other field is skipped by wire type, which is what
makes an unknown future field harmless rather than fatal.

The format: a message is a sequence of (key, value) pairs where the key is a varint holding
`field_number << 3 | wire_type`. Wire type 0 is a varint, 2 is length-delimited, 1 and 5
are fixed 64- and 32-bit. Field numbers are from `scip.proto` and were verified by oxn
against a real index produced by scip-python 0.6.6.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

VARINT = 0
FIXED64 = 1
LENGTH_DELIMITED = 2
FIXED32 = 5


class WireFormatError(ValueError):
    """Raised when the bytes are not a well-formed protobuf message."""


def read_varint(buffer: bytes, offset: int) -> tuple[int, int]:
    """Decode a base-128 varint. Returns ``(value, next offset)``."""
    result = 0
    shift = 0
    while True:
        if offset >= len(buffer):
            raise WireFormatError("truncated varint")
        byte = buffer[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 70:
            raise WireFormatError("varint too long")


def iter_fields(buffer: bytes) -> Iterator[tuple[int, int, bytes | int]]:
    """Yield ``(field number, wire type, value)`` for every field in a message.

    Length-delimited fields yield their raw bytes; varints yield an int; fixed-width fields
    yield their raw bytes. Unknown fields are yielded too -- the caller ignores what it does
    not recognise, which is where forward compatibility comes from.
    """
    offset = 0
    length = len(buffer)
    while offset < length:
        key, offset = read_varint(buffer, offset)
        number, wire_type = key >> 3, key & 0x07

        if wire_type == VARINT:
            value, offset = read_varint(buffer, offset)
            yield number, wire_type, value
        elif wire_type == LENGTH_DELIMITED:
            size, offset = read_varint(buffer, offset)
            end = offset + size
            if end > length:
                raise WireFormatError("length-delimited field overruns the message")
            yield number, wire_type, buffer[offset:end]
            offset = end
        elif wire_type in (FIXED64, FIXED32):
            size = 8 if wire_type == FIXED64 else 4
            if offset + size > length:
                raise WireFormatError("fixed-width field overruns the message")
            yield number, wire_type, buffer[offset : offset + size]
            offset += size
        else:
            raise WireFormatError(f"unsupported wire type {wire_type}")


def read_packed_varints(buffer: bytes) -> list[int]:
    """Decode a packed repeated varint field, as SCIP uses for ranges."""
    values: list[int] = []
    offset = 0
    while offset < len(buffer):
        value, offset = read_varint(buffer, offset)
        values.append(value)
    return values


def text(value: bytes | int) -> str:
    """A length-delimited field as UTF-8, replacing anything invalid."""
    if isinstance(value, int):  # pragma: no cover - schema mismatch
        return str(value)
    return value.decode("utf-8", "replace")


# --- Index ---
_DOCUMENTS = 2
_EXTERNAL_SYMBOLS = 3
# --- Document ---
_RELATIVE_PATH = 1
_OCCURRENCES = 2
_SYMBOLS = 3
# --- Occurrence ---
_RANGE = 1
_SYMBOL = 2
_SYMBOL_ROLES = 3
# --- SymbolInformation ---
_INFO_SYMBOL = 1
_RELATIONSHIPS = 4
_KIND = 5
_DISPLAY_NAME = 6
# --- Relationship ---
_REL_SYMBOL = 1
_IS_IMPLEMENTATION = 3
_IS_TYPE_DEFINITION = 4

#: ``symbol_roles`` is a bitfield. Definition is the bit that maps an occurrence to the
#: place a symbol is *declared*, which is what the range join needs.
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
ROLE_WRITE = 0x4
ROLE_READ = 0x8


@dataclass(frozen=True, slots=True)
class ScipOccurrence:
    """One symbol reference at a source position.

    Positions are **0-based** and character-oriented, matching tree-sitter's ``start_point``.
    """

    symbol: str
    start_line: int
    start_char: int
    end_line: int
    end_char: int
    roles: int

    @property
    def is_definition(self) -> bool:
        return bool(self.roles & ROLE_DEFINITION)

    @property
    def is_import(self) -> bool:
        return bool(self.roles & ROLE_IMPORT)


@dataclass(frozen=True, slots=True)
class ScipRelationship:
    """A typed link between symbols. ``is_implementation`` gives inheritance directly."""

    symbol: str
    is_implementation: bool = False
    is_type_definition: bool = False


@dataclass(frozen=True, slots=True)
class ScipSymbol:
    """What the indexer knows about one declared symbol."""

    symbol: str
    display_name: str = ""
    kind: int = 0
    relationships: tuple[ScipRelationship, ...] = ()


@dataclass
class ScipDocument:
    """One indexed file."""

    relative_path: str
    occurrences: list[ScipOccurrence] = field(default_factory=list)
    symbols: list[ScipSymbol] = field(default_factory=list)

    def definitions(self) -> Iterator[ScipOccurrence]:
        return (occurrence for occurrence in self.occurrences if occurrence.is_definition)


@dataclass
class ScipIndex:
    """A parsed SCIP index."""

    documents: list[ScipDocument] = field(default_factory=list)
    external_symbols: list[ScipSymbol] = field(default_factory=list)
    #: Documents dropped because another already covered that path.
    #:
    #: **One index can describe a file twice.** A SCIP indexer is given *projects*, not files,
    #: and a file included by two of them is emitted once per project -- `scip-typescript`
    #: over `tsconfig.json` and `tsconfig.spec.json` produces 1,840 documents for 1,444 paths
    #: on `typescript-nest`, because both configs include `integration/**`. The store is safe
    #: either way (`put_symbols` and `put_edges` replace by path), but every counter on
    #: `IngestReport` accumulates per document, so the copies would inflate `call_sites` and
    #: `symbols` for 396 files while the ratios quietly stayed plausible.
    #:
    #: Kept as a number rather than dropped in silence: it says a project's configs overlap,
    #: which is a fact about that project and not a fault. On nest's 396 the two copies carry
    #: identical occurrence counts, so which one survives does not matter -- the first does,
    #: because that is reproducible.
    duplicate_documents: int = 0

    @property
    def occurrence_count(self) -> int:
        return sum(len(document.occurrences) for document in self.documents)

    def by_path(self) -> dict[str, ScipDocument]:
        return {document.relative_path: document for document in self.documents}


def load_index(path: Path | str) -> ScipIndex:
    """Parse a ``.scip`` file."""
    return parse_index(Path(path).read_bytes())


def parse_index(data: bytes) -> ScipIndex:
    """Parse a SCIP index, keeping one document per path. See `ScipIndex.duplicate_documents`."""
    index = ScipIndex()
    seen: set[str] = set()
    for number, _, value in iter_fields(data):
        if number == _DOCUMENTS and isinstance(value, bytes):
            document = _parse_document(value)
            if document.relative_path in seen:
                index.duplicate_documents += 1
                continue
            seen.add(document.relative_path)
            index.documents.append(document)
        elif number == _EXTERNAL_SYMBOLS and isinstance(value, bytes):
            index.external_symbols.append(_parse_symbol(value))
    return index


def _parse_document(data: bytes) -> ScipDocument:
    document = ScipDocument(relative_path="")
    for number, _, value in iter_fields(data):
        if number == _RELATIVE_PATH and isinstance(value, bytes):
            document.relative_path = text(value)
        elif number == _OCCURRENCES and isinstance(value, bytes):
            occurrence = _parse_occurrence(value)
            if occurrence is not None:
                document.occurrences.append(occurrence)
        elif number == _SYMBOLS and isinstance(value, bytes):
            document.symbols.append(_parse_symbol(value))
    return document


def _parse_occurrence(data: bytes) -> ScipOccurrence | None:
    """One occurrence: a symbol, where it appears, and the role it plays there."""
    span, symbol, roles = _occurrence_fields(data)
    if not symbol or not span:
        return None
    bounds = _span_bounds(span)
    if bounds is None:  # pragma: no cover - malformed
        return None
    start_line, start_char, end_line, end_char = bounds
    return ScipOccurrence(
        symbol=symbol,
        start_line=start_line,
        start_char=start_char,
        end_line=end_line,
        end_char=end_char,
        roles=roles,
    )


def _occurrence_fields(data: bytes) -> tuple[list[int], str, int]:
    """The three fields an occurrence carries, in whichever order they were written."""
    span: list[int] = []
    symbol = ""
    roles = 0
    for number, _wire_type, value in iter_fields(data):
        if number == _RANGE:
            if isinstance(value, bytes):
                span = read_packed_varints(value)
            elif isinstance(value, int):
                span.append(value)  # unpacked encoding, still legal
        elif number == _SYMBOL and isinstance(value, bytes):
            symbol = text(value)
        elif number == _SYMBOL_ROLES and isinstance(value, int):
            roles = value
    return span, symbol, roles


def _span_bounds(span: list[int]) -> tuple[int, int, int, int] | None:
    """A range is 3 elements when it stays on one line and 4 when it spans lines.

    Handling only the 4-element form silently drops most occurrences in real code.
    """
    if len(span) == 3:
        start_line, start_char, end_char = span
        return start_line, start_char, start_line, end_char
    if len(span) >= 4:
        start_line, start_char, end_line, end_char = span[:4]
        return start_line, start_char, end_line, end_char
    return None


def _parse_symbol(data: bytes) -> ScipSymbol:
    symbol = ""
    display_name = ""
    kind = 0
    relationships: list[ScipRelationship] = []
    for number, _, value in iter_fields(data):
        if number == _INFO_SYMBOL and isinstance(value, bytes):
            symbol = text(value)
        elif number == _DISPLAY_NAME and isinstance(value, bytes):
            display_name = text(value)
        elif number == _KIND and isinstance(value, int):
            kind = value
        elif number == _RELATIONSHIPS and isinstance(value, bytes):
            relationships.append(_parse_relationship(value))
    return ScipSymbol(symbol, display_name, kind, tuple(relationships))


def _parse_relationship(data: bytes) -> ScipRelationship:
    symbol = ""
    is_implementation = False
    is_type_definition = False
    for number, _, value in iter_fields(data):
        if number == _REL_SYMBOL and isinstance(value, bytes):
            symbol = text(value)
        elif number == _IS_IMPLEMENTATION and isinstance(value, int):
            is_implementation = bool(value)
        elif number == _IS_TYPE_DEFINITION and isinstance(value, int):
            is_type_definition = bool(value)
    return ScipRelationship(symbol, is_implementation, is_type_definition)


# ---- what the code graph keeps of an index --------------------------------------------------------

_DESCRIPTOR = re.compile(r"(?:`([^`]+)`|([A-Za-z_$][\w$]*))(?:\([^)]*\))?[.#:/!]*$")
_ACCESSOR = re.compile(r"^<(?:get|set)>")


def symbol_name(symbol: str) -> str:
    """The name a SCIP symbol ends in: `... pkg/a/Client#send().` -> `send`; an accessor
    `<get>size` -> `size`. '' for a symbol local to its file."""
    if symbol.startswith("local "):
        return ""
    match = _DESCRIPTOR.search(symbol.rsplit(" ", 1)[-1])
    return _ACCESSOR.sub("", (match.group(1) or match.group(2))) if match else ""


def tool_name(data: bytes) -> str:
    """The indexer that wrote the index, from its metadata; '' when it does not say."""
    try:
        for number, _, value in iter_fields(data):
            if number == 1 and isinstance(value, bytes):                # Index.metadata
                for inner, _, info in iter_fields(value):
                    if inner == 2 and isinstance(info, bytes):          # Metadata.tool_info
                        for last, _, name in iter_fields(info):
                            if last == 1 and isinstance(name, bytes):   # ToolInfo.name
                                return name.decode("utf-8", "replace")[:80]
                return ""
    except WireFormatError:
        pass
    return ""


@dataclass(frozen=True)
class Reduced:
    """One document, reduced to what answers "what does this refer to": lines and columns count from 0."""
    path: str
    references: list[tuple[int, int, str, int, str]]   # (line, column, the defining file, the line of the definition's name, its name)
    outside: list[tuple[int, int]]                     # where it refers to something no document of the index defines: a library
    depends_on: list[str]                              # the other documents whose definitions it refers to


def is_inside(path: str) -> bool:
    """Whether a document's path is a plain path under the root: an index is data from elsewhere."""
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and "\\" not in path and "\0" not in path and ".." not in parts and "" not in parts


def reduce(index: ScipIndex) -> tuple[list[Reduced], int]:
    """Every document's references resolved to where they are defined, and how many documents were
    left out because their path leaves the tree."""
    sites: dict[tuple[str, str], tuple[str, int]] = {}
    kept_documents = [document for document in index.documents if is_inside(document.relative_path)]
    for document in kept_documents:
        for occurrence in document.definitions():
            scope = document.relative_path if occurrence.symbol.startswith("local ") else ""
            sites.setdefault((scope, occurrence.symbol), (document.relative_path, occurrence.start_line))
    out: list[Reduced] = []
    for document in kept_documents:
        references: list[tuple[int, int, str, int, str]] = []
        outside: list[tuple[int, int]] = []
        depends: set[str] = set()
        for occurrence in document.occurrences:
            if occurrence.is_definition:
                continue
            local = occurrence.symbol.startswith("local ")
            site = sites.get((document.relative_path if local else "", occurrence.symbol))
            if site is None:
                if not local:
                    outside.append((occurrence.start_line, occurrence.start_char))
                continue
            references.append((occurrence.start_line, occurrence.start_char, site[0], site[1], symbol_name(occurrence.symbol)))
            if site[0] != document.relative_path:
                depends.add(site[0])
        out.append(Reduced(document.relative_path, references, outside, sorted(depends)))
    return out, len(index.documents) - len(kept_documents)

