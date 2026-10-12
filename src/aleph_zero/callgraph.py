"""The code graph of a view: what is kept of each file, and the questions asked of it.

`graph.py` answers which definition a call reaches, from facts per file. This module keeps
those facts beside the index, one row per blob, and asks the graph two questions: who calls
a definition, and what a definition calls.

The facts are read when a graph question is first asked of a view, not when a blob is
indexed. Reading them at indexing time added a third to the time to index Django and to the
size of its index, for every user, whether or not a graph question is ever asked; this way
searching pays nothing. No edge is stored either: an edge depends on the other files of the
view (what an import leads to, what a class is built on), so it is worked out when asked.

Which files call a name is asked of the text index, which already knows which chunks
mention an identifier: a file that calls `helper`, or imports it under another name,
mentions it.
"""
from __future__ import annotations

import json
import re
import time
import zlib
from dataclasses import dataclass

import apsw

from . import filters, gitio, lang, scip, symbols
from .graph import MANIFESTS, Defined, Edge, FileFacts, Graph, Source, Target, _group
from .indexer import Index, symbol_key
from .lang import parse
from .lang.bindings import Binding, facts
from .lang.calls import FAMILIES, Call, calls
from .lang.definitions import definitions
from .store import transaction
from .view import View

GRAPH_VERSION = 5           # bump when what the readers extract changes: every row is then read again
PREPARE_BATCH = 64          # blobs read between two commits
MENTION_LIMIT = 3000        # chunks taken from the text index for one name
DEFINITION_LIMIT = 8        # definitions of one name that are answered for
MANIFEST_LIMIT = 2000
RENAMINGS = 2               # how many times a definition may change its name on the way to a caller and still be followed
NAMES_FOLLOWED = 12         # names looked up for one question, the definition's own included
_WORD = re.compile(r"\w")
MANIFEST_BYTES = 262_144
_TYPES = frozenset({"class", "interface", "enum", "type"})
_EMPTY = (zlib.compress(b"[[],[],[],[]]", 1), zlib.compress(b"[]", 1))
_CODE = sorted(FAMILIES & set(lang.PROFILES))
_IN_CODE = "(" + ",".join("?" * len(_CODE)) + ")"
_PENDING = (
    "SELECT v.blob_oid, v.family FROM temp.view_blobs v "
    "JOIN blobs b ON b.blob_oid = v.blob_oid AND b.family = v.family "
    "LEFT JOIN graph_facts g ON g.blob_oid = v.blob_oid AND g.family = v.family AND g.version = ? "
    f"WHERE g.blob_oid IS NULL AND v.family IN {_IN_CODE}"
)
_now = time.monotonic       # looked at through this name so that a test can move the clock


@dataclass(frozen=True)
class Reference:
    """One call, and one definition it may reach."""
    path: str            # where the call is written
    line: int
    written: str         # the call as written: `self.store.save`
    caller: str          # the definition the call sits in, qualified; '' at file level
    target_path: str
    target_line: int
    target: str          # the definition reached, qualified within its file
    kind: str
    level: str           # 'L0' the same file, 'L1' another
    provenance: str      # 'syntactic', 'import-resolved', or 'name': found by its name alone
    candidates: int      # how many definitions this call may reach; 1 is not yet certain: see `certain`
    certain: bool        # one candidate, not by its name alone, and nothing else in doubt
    note: str = ""       # why a lone candidate is not certain, when the reason is not its name
    downgraded: str = ""  # why this was not answered from the imported compiler index, when one covers the file


@dataclass(frozen=True)
class Answer:
    symbol: str
    definitions: list[symbols.SymbolHit]
    callers: list[Reference]
    callees: list[Reference]
    unanswered: int = 0          # calls inside the definitions that reach nothing in the view: libraries, local functions
    pending: int = 0             # files of the view not indexed yet
    graph_pending: int = 0       # indexed files whose calls and imports are not read yet
    late: bool = False
    elapsed_ms: int = 0
    truncated: bool = False      # more callers may exist than were looked at
    direction: str = "both"      # what was asked: 'callers', 'callees' or 'both'


def pending(index: Index) -> list[tuple[str, str]]:
    """The indexed code blobs of the synced view whose graph facts are not read yet."""
    return [(oid, family) for oid, family in index.conn.execute(_PENDING, (GRAPH_VERSION, *_CODE))]


def coverage(index: Index) -> tuple[int, int]:
    """(code blobs of the view whose graph facts are read, those that are not)."""
    ready = index.conn.execute(
        "SELECT count(*) FROM graph_facts g JOIN temp.view_blobs v ON v.blob_oid = g.blob_oid AND v.family = g.family "
        "WHERE g.version = ?", (GRAPH_VERSION,)).fetchall()[0][0]
    return ready, len(pending(index))


def prepare(index: Index, view: View, budget_seconds: float, stop=None) -> int:
    """Read and store the graph facts of the view's blobs that lack them, for as long as the
    budget allows. Returns how many are still to be read."""
    todo = pending(index)
    if not todo:
        return 0
    deadline = _now() + budget_seconds
    with gitio.Readers() as reader:
        for start in range(0, len(todo), PREPARE_BATCH):
            if _now() >= deadline or (stop is not None and stop()):
                break
            batch = todo[start:start + PREPARE_BATCH]
            rows = [(key, _read(index, view, reader, key)) for key in batch]
            with transaction(index.conn):
                for (oid, family), found in rows:
                    if found is None:
                        continue    # git does not have the content right now: nothing is recorded without it
                    try:
                        index.conn.execute(
                            "INSERT INTO graph_facts(blob_oid, family, version, shape, calls) VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(blob_oid, family) DO UPDATE SET version = excluded.version, "
                            "shape = excluded.shape, calls = excluded.calls", (oid, family, GRAPH_VERSION, *found))
                    except apsw.ConstraintError:
                        pass        # another session collected the blob meanwhile
    return len(pending(index))      # also what git could not supply: unread is unread, and the answer says so


def _read(index: Index, view: View, reader: gitio.Readers, key: tuple[str, str]) -> tuple[bytes, bytes] | None:
    oid, family = key
    for path in index.paths_for(oid, family):
        data = view.read(path, reader, filters.MAX_BYTES)
        if data is not None:
            break
    else:
        return None
    if isinstance(data, gitio.TooLarge):
        return _EMPTY
    _, text = filters.classify(family, data)
    profile = lang.PROFILES.get(family)
    if text is None or profile is None:
        return _EMPTY
    return extract(profile, text)


def extract(profile: lang.LanguageProfile, text: str) -> tuple[bytes, bytes]:
    """(shape, calls) of one file as stored: see the `graph_facts` table."""
    source = text.replace("\r\n", "\n").encode("utf-8", "surrogateescape")    # as the chunker read it: the symbols' order is the callers' key
    try:
        tree = parse.tree(profile, source)
        found = definitions(profile, tree, source)
        sites = calls(profile, tree, source, found)
        bound, built_on, typed = facts(profile, tree, source)
    except Exception:  # noqa: BLE001 - a grammar that fails to load, or a shape a reader does not know
        return _EMPTY
    # The last list is where each definition starts and ends, in the symbols' order: the index's symbols
    # table keeps only where one starts, and an answer taken from a compiler index must fall inside one.
    shape = [[[b.local, b.source, b.name, b.level, b.kind] for b in bound], built_on, typed,
             [[d.start_line, d.end_line] for d in found]]
    # A plain call of a local name (a parameter, a closure) can never be answered: not kept.
    kept = [[c.name, c.receiver, c.line, c.column, c.caller, c.via, c.head, c.receiver_type, c.arity, c.column16, c.column32,
             [list(lines) for lines in c.inner]]
            for c in sites if c.via or c.head != "local"]
    pack = lambda value: zlib.compress(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogateescape"), 1)  # noqa: E731
    return pack(shape), pack(kept)


def _unpack(blob: bytes):
    return json.loads(zlib.decompress(blob).decode("utf-8", "surrogateescape"))


class _CodePaths:
    """The code files of a view, as a collection that is not a copy of them."""

    def __init__(self, view: View) -> None:
        self._files = view.files

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and path in self._files and filters.family(path) in FAMILIES

    def __iter__(self):
        return (path for path in self._files if filters.family(path) in FAMILIES)

    def __len__(self) -> int:
        return sum(1 for _ in self)


class Indexed(Source):
    """A view's facts, read from its index one file at a time as a question reaches them. A file whose
    graph facts are not read yet takes part with its definitions alone."""
    NAMES_KEPT = 1000

    def __init__(self, index: Index, view: View) -> None:
        self._index, self._view = index, view
        self._paths = _CodePaths(view)
        self._names: dict[tuple[str, str], list[Target]] = {}
        self._types: dict[tuple[str, str], list[str]] = {}

    def paths(self) -> _CodePaths:
        return self._paths

    def facts(self, path: str) -> FileFacts | None:
        oid, family = self._view.files.get(path), filters.family(path)
        if oid is None or family not in FAMILIES:
            return None
        conn = self._index.conn
        rows = conn.execute("SELECT name, qualified, kind, line FROM symbols WHERE blob_oid = ? AND family = ? ORDER BY ord",
                            (oid, family)).fetchall()
        shape = conn.execute("SELECT shape FROM graph_facts WHERE blob_oid = ? AND family = ? AND version = ?",
                             (oid, family, GRAPH_VERSION)).fetchall()
        bound, built_on, typed, spans = _unpack(shape[0][0]) if shape else ([], [], [], [])
        ends = [end for _, end in spans] if len(spans) == len(rows) else [line for _, _, _, line in rows]
        defined = [Defined(kind, name, qualified, "", line, end) for (name, qualified, kind, line), end in zip(rows, ends)]
        return FileFacts(family, defined, [Binding(*row) for row in bound], [tuple(row) for row in built_on],
                         [tuple(row) for row in typed])

    def named(self, group: str, name: str) -> list[Target]:
        key = (group, name)
        if key not in self._names:
            if len(self._names) >= self.NAMES_KEPT:
                self._names.clear()
            found: list[Target] = []
            for oid, family, qualified, kind, line in self._index.conn.execute(
                    "SELECT s.blob_oid, s.family, s.qualified, s.kind, s.line FROM symbols s "
                    "JOIN temp.view_blobs v ON v.blob_oid = s.blob_oid AND v.family = s.family "
                    "WHERE s.name_key = ? AND s.name = ? AND s.kind != 'namespace' ORDER BY s.blob_oid, s.family, s.ord",
                    (symbol_key(name), name)):
                if family in FAMILIES and _group(family) == group:
                    found += [Target(path, name, qualified, kind, line, line) for path in self._index.paths_for(oid, family)]
            self._names[key] = found
        return self._names[key]

    def type_files(self, group: str, simple: str) -> list[str]:
        key = (group, simple)
        if key not in self._types:
            if len(self._types) >= self.NAMES_KEPT:
                self._types.clear()
            self._types[key] = [path for oid, family in mentions(self._index, simple)[0]
                                if family in FAMILIES and _group(family) == group for path in self._index.paths_for(oid, family)]
        return self._types[key]

    def signature(self, target: Target) -> str:
        oid = self._view.files.get(target.path)
        rows = self._index.conn.execute(
            "SELECT signature FROM symbols WHERE blob_oid = ? AND family = ? AND qualified = ? AND line = ? LIMIT 1",
            (oid, filters.family(target.path), target.qualified, target.start_line)).fetchall()
        return rows[0][0] if rows else ""


def load(index: Index, view: View) -> Graph:
    """The graph of the synced view. It reads a file from the index when a question first reaches it."""
    manifests: dict[str, bytes] = {}
    named = [path for path in view.files if path.rsplit("/", 1)[-1] in MANIFESTS][:MANIFEST_LIMIT]
    if named:
        with gitio.Readers() as reader:
            for path in named:
                data = view.read(path, reader, MANIFEST_BYTES)
                if isinstance(data, bytes):
                    manifests[path] = data
    return Graph(Indexed(index, view), manifests)


def mentions(index: Index, name: str) -> tuple[list[tuple[str, str]], bool]:
    """The blobs of the view whose text mentions `name` as a word, and whether the list was cut short."""
    expression = '"' + name.replace('"', '""') + '"'
    # The unary plus keeps SQLite from running one text match per chunk of the view (see lexical.py).
    chunk_ids = [row[0] for row in index.conn.execute(
        "SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ? AND +rowid IN (SELECT chunk_id FROM temp.view_chunks) LIMIT ?",
        (expression, MENTION_LIMIT + 1))]
    cut = len(chunk_ids) > MENTION_LIMIT
    found: dict[tuple[str, str], None] = {}
    for start in range(0, min(len(chunk_ids), MENTION_LIMIT), 500):
        some = chunk_ids[start:start + 500]
        for oid, family in index.conn.execute(
                "SELECT DISTINCT bc.blob_oid, bc.family FROM blob_chunks bc "
                "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid AND v.family = bc.family "
                f"WHERE bc.chunk_id IN ({','.join('?' * len(some))})", some):
            found[(oid, family)] = None
    return list(found), cut


def _calls(index: Index, key: tuple[str, str]) -> list[Call]:
    rows = index.conn.execute("SELECT calls FROM graph_facts WHERE blob_oid = ? AND family = ? AND version = ?",
                              (*key, GRAPH_VERSION)).fetchall()
    return [Call(*row[:11], tuple((first, last) for first, last in row[11])) for row in _unpack(rows[0][0])] if rows else []


def resolve(graph: Graph, compiled, path: str, call: Call) -> tuple[Edge | None, str]:
    """What a call reaches, and why the answer is not the compiler's when an imported index covers the
    file but may not be believed. A believed document answers alone: what it places outside the tree
    is a call into a library, and gets no answer; where it says nothing, the resolver does."""
    state = compiled.status(path) if compiled is not None else None
    if state != "":
        return graph.resolve(path, call), state or ""
    said = compiled.target(path, call.line - 1, (call.column, call.column16, call.column32), call.name)
    if said is False:
        return None, ""
    if said is not None:
        target = _defined_at(graph, *said)
        if target is not None:
            return Edge((target,), "L2", "scip", "compiler index"), ""
        found = graph.resolve(path, call)
        if found is not None and found.rule == "inner function" and any(
                each.path == said[0] and each.start_line - 1 <= said[1] <= each.end_line - 1 for each in found.targets):
            return found, ""    # a function inside a function: the resolver reaches the one the index names
        return found, ("the compiler index points at something that is not a recorded definition "
                       "(a function or class inside a function)")
    return graph.resolve(path, call), ""


def _defined_at(graph: Graph, path: str, line: int, name: str) -> Target | None:
    """The definition the index points at. It gives the line of the definition's name (from 0) and its name;
    the graph knows each definition's lines (from 1). The answer is the innermost definition of that name
    that holds the line: a namesake further up is another thing, and what the index means may be something
    not recorded at all (a class inside a function), which is then no answer."""
    of_file = graph.files.get(path)
    if of_file is None or not name:
        return None
    if name.endswith("#"):      # a constructor: the type itself, or the method that constructs it
        kind = name[:-1]
        fits = lambda each: each.name == kind or (each.name in scip.CONSTRUCTORS and each.qualified.split(".")[-2:-1] == [kind])  # noqa: E731
    else:
        fits = lambda each: each.name == name  # noqa: E731
    holding = [each for each in of_file.definitions if each.start_line <= line + 1 <= each.end_line and fits(each)]
    return max(holding, key=lambda each: each.start_line) if holding else None


def _reference(path: str, of_file: FileFacts, call: Call, edge: Edge, target: Target, downgraded: str = "") -> Reference:
    joined = "::" if call.via == "path" else "."
    caller = of_file.definitions[call.caller].qualified if call.caller is not None and call.caller < len(of_file.definitions) else ""
    return Reference(path, call.line, f"{call.receiver}{joined}{call.name}" if call.receiver else call.name, caller,
                     target.path, target.start_line, target.qualified, target.kind, edge.level, edge.provenance,
                     len(edge.targets), edge.certain, edge.doubt, downgraded)


def callers(index: Index, graph: Graph, hits: list[symbols.SymbolHit], compiled=None) -> tuple[list[Reference], bool]:
    """Every call in the view that may reach one of `hits`: the certain ones first. Also whether the
    answer may be short: the text index named more places than were looked at, or cannot look the name up.

    A caller may know the definition under another name: `from .util import slugify as slug` in a
    package's `__init__` makes `slug` the name its users write. The files that mention the definition's
    own name say what else it is called, and those names are looked up too."""
    wanted = {(hit.path, hit.qualified, hit.line) for hit in hits}
    out: list[Reference] = []
    truncated = False
    waiting = [(name, 0) for name in sorted({hit.name for hit in hits})]
    looked: set[str] = set()
    counted: set[tuple[str, int, int, str]] = set()
    while waiting:
        name, renamings = waiting.pop(0)
        if name in looked:
            continue
        looked.add(name)
        if not _WORD.search(name):
            truncated = True    # `$`: nothing the text index keeps as a word
            continue
        keys, cut = mentions(index, name)
        truncated = truncated or cut
        for key in keys:
            sites = _calls(index, key)
            for path in index.paths_for(*key):
                of_file = graph.files.get(path)
                if of_file is None:
                    continue
                given = {b.local for b in of_file.bindings if b.name == name and b.kind in ("import", "reexport", "export")} - {"*", "default"}
                if renamings < RENAMINGS and len(looked) + len(waiting) < NAMES_FOLLOWED:
                    waiting += [(other, renamings + 1) for other in sorted(given - looked - {name})]
                written = {name} | {b.local for b in of_file.bindings if b.name == name and b.kind == "import"}
                for call in sites:
                    if call.name not in written:
                        continue
                    edge, downgraded = resolve(graph, compiled, path, call)
                    for target in (edge.targets if edge is not None else ()):
                        site = (path, call.line, call.column, target.qualified)
                        if (target.path, target.qualified, target.start_line) in wanted and site not in counted:
                            counted.add(site)
                            out.append(_reference(path, of_file, call, edge, target, downgraded))
    out.sort(key=lambda r: (not r.certain, r.candidates, r.path, r.line, r.target_path, r.target))
    return out, truncated


def callees(index: Index, view: View, graph: Graph, hits: list[symbols.SymbolHit], compiled=None) -> tuple[list[Reference], int]:
    """What each of `hits` calls (for a type: what its members call), in source order, and how many
    of its calls reach nothing in the view."""
    out: list[Reference] = []
    unanswered = 0
    seen: set[tuple[str, int]] = set()
    for hit in hits:
        oid, of_file = view.files.get(hit.path), graph.files.get(hit.path)
        if oid is None or of_file is None:
            continue
        inside = [index for index, definition in enumerate(of_file.definitions)
                  if (definition.qualified == hit.qualified and definition.start_line == hit.line)
                  or (hit.kind in _TYPES and definition.qualified.startswith(hit.qualified + "."))]
        for call in _calls(index, (oid, of_file.family)):
            if call.caller not in inside or (hit.path, call.column + call.line * 100_000) in seen:
                continue
            seen.add((hit.path, call.column + call.line * 100_000))
            edge, downgraded = resolve(graph, compiled, hit.path, call)
            if edge is None:
                unanswered += 1
            for target in (edge.targets if edge is not None else ()):
                out.append(_reference(hit.path, of_file, call, edge, target, downgraded))
    return out, unanswered


def text(answer: Answer) -> str:
    """The answer for a reader: each definition, then who calls it and what it calls. Certain answers
    first; what was found by name alone is listed apart and says how many candidates it is one of."""
    if not answer.definitions:
        lines = [f'no definition named "{answer.symbol}" in this worktree']
    else:
        lines = [f"{hit.path}:{hit.line}  {hit.kind}  {hit.qualified}" for hit in answer.definitions]
        several = len(answer.definitions) > 1
        for title, found, callers in (("callers", answer.callers, True), ("callees", answer.callees, False)):
            if answer.direction not in ("both", title):
                continue
            if not found:
                lines.append(f"{title}: none in this worktree")
                continue
            certain = [item for item in found if item.certain]
            possible = [item for item in found if not item.certain]
            lines.append(f"{title}: {len(certain)} certain, {len(possible)} possible")
            for group in (certain, possible):
                if group is possible and possible:
                    lines.append("  possible:")
                for item in group:
                    where = f"  {item.path}:{item.line}  " + (f"in {item.caller}  " if callers and item.caller else "") + f"{item.written}()"
                    if not callers or several:
                        where += f"  ->  {item.target_path}:{item.target_line}  {item.target}"
                    if item.certain:
                        why = "  (compiler index)" if item.level == "L2" else ""
                    elif item.provenance == "name":
                        why = f"  by name alone, 1 of {item.candidates}: nothing states the receiver's type"
                    elif item.candidates > 1:
                        why = f"  1 of {item.candidates} candidates"
                    else:
                        why = f"  {item.note}"
                    lines.append(where + why)
    stale = sorted({item.downgraded for item in (*answer.callers, *answer.callees) if item.downgraded})
    if stale:
        lines.append(f"note: an imported compiler index covers these files but could not be used here, so the resolver "
                     f"answered: {stale[0]}" + (f" (and {len(stale) - 1} more reasons)" if len(stale) > 1 else "")
                     + ". To have exact answers again: `aleph scip run`, or `aleph scip import` a new index")
    if answer.unanswered:
        lines.append(f"note: {answer.unanswered} calls inside it reach nothing in this worktree (libraries, local functions)")
    if answer.truncated:
        lines.append("note: the answer may be short: more definitions or calls than are listed, or a name that cannot be looked up")
    if answer.graph_pending:
        lines.append(f"note: the calls of {answer.graph_pending} files are not read yet; ask again for a complete answer")
    if answer.pending:
        lines.append(f"note: {answer.pending} files are not indexed yet; results may be incomplete")
    return "\n".join(lines)
