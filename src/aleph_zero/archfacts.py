"""Architectural facts of a view: the routes it serves, its models and tables, the environment
names it reads, its tasks and services (M8).

What one file states does not depend on any other file, so it is kept per blob, like the code
graph's facts, and like them it is read when first asked for: an index that is only searched
pays nothing. An answer is the facts of the files the view holds now, each with its path and line.
"""
from __future__ import annotations

import json
import time
import zlib
from dataclasses import dataclass, field

import apsw

from . import filters, gitio, lang
from .indexer import Index
from .lang import parse
from .lang.facts import Fact, facts, states_facts, text_facts
from .store.db import transaction
from .view import View

FACTS_VERSION = 1           # bump when what the readers extract changes: every row is then read again
PREPARE_BATCH = 64          # blobs read between two commits
KINDS = ("route", "model", "table", "env", "task", "service", "rpc")
_CODE = sorted({"typescript", "tsx", "javascript", "python", "go", "rust", "java"} & set(lang.PROFILES))
_TEXT = "text"              # the family of a Prisma schema, an example environment file, a .proto
_READ = "(" + ",".join("?" * (len(_CODE) + 1)) + ")"
_PENDING = (
    "SELECT v.blob_oid, v.family FROM temp.view_blobs v "
    "JOIN blobs b ON b.blob_oid = v.blob_oid AND b.family = v.family "
    "LEFT JOIN arch_facts a ON a.blob_oid = v.blob_oid AND a.family = v.family AND a.version = ? "
    f"WHERE a.blob_oid IS NULL AND v.family IN {_READ}"
)
_NONE = b""                 # a file that states nothing: most of them
_now = time.monotonic       # looked at through this name so that a test can move the clock


@dataclass(frozen=True)
class Found:
    kind: str
    name: str
    path: str
    line: int
    detail: str


@dataclass(frozen=True)
class Answer:
    kind: str                    # what was asked for; '' is every kind
    like: str
    facts: list[Found]           # listed when a kind or a name was asked for; by name, then path
    counts: dict[str, int] = field(default_factory=dict)   # of every kind in the view, whatever was asked
    pending: int = 0             # files of the view not indexed yet
    unread: int = 0              # indexed files whose facts are not read yet
    late: bool = False
    elapsed_ms: int = 0
    truncated: bool = False      # more match than are listed


def pending(index: Index) -> list[tuple[str, str]]:
    """The indexed blobs of the synced view that may state facts and are not read yet. Of the
    files that are not code, only those whose name says what they are."""
    return [(oid, family) for oid, family in index.conn.execute(_PENDING, (FACTS_VERSION, *_CODE, _TEXT))
            if family != _TEXT or _stating(index, oid)]


def _stating(index: Index, oid: str) -> list[str]:
    return [path for path in index.paths_for(oid, _TEXT) if states_facts(path)]


def prepare(index: Index, view: View, budget_seconds: float, stop=None) -> int:
    """Read and store the facts of the view's blobs that lack them, for as long as the budget
    allows. Returns how many are still to be read."""
    todo = pending(index)
    if not todo:
        return 0
    deadline = _now() + budget_seconds
    with gitio.Readers() as reader:
        for start in range(0, len(todo), PREPARE_BATCH):
            if _now() >= deadline or (stop is not None and stop()):
                break
            rows = [(key, _read(index, view, reader, key)) for key in todo[start:start + PREPARE_BATCH]]
            with transaction(index.conn):
                for (oid, family), found in rows:
                    if found is None:
                        continue    # git does not have the content right now: nothing is recorded without it
                    try:
                        index.conn.execute(
                            "INSERT INTO arch_facts(blob_oid, family, version, facts) VALUES (?, ?, ?, ?) "
                            "ON CONFLICT(blob_oid, family) DO UPDATE SET version = excluded.version, facts = excluded.facts",
                            (oid, family, FACTS_VERSION, found))
                    except apsw.ConstraintError:
                        pass        # another session collected the blob meanwhile
    return len(pending(index))


def _read(index: Index, view: View, reader: gitio.Readers, key: tuple[str, str]) -> bytes | None:
    oid, family = key
    paths = _stating(index, oid) if family == _TEXT else index.paths_for(oid, family)
    for path in paths:
        data = view.read(path, reader, filters.MAX_BYTES)
        if data is not None:
            break
    else:
        return None
    if isinstance(data, gitio.TooLarge):
        return _NONE
    _, text = filters.classify(family, data)
    if text is None:
        return _NONE
    return pack(extract(family, path, text))


def extract(family: str, path: str, text: str) -> list[Fact]:
    """The facts of one file. A reader that fails on a shape it does not know states nothing."""
    text = text.replace("\r\n", "\n")
    try:
        if family == _TEXT:
            return text_facts(path, text)
        profile = lang.PROFILES[family]
        source = text.encode("utf-8", "surrogateescape")
        return facts(profile, parse.tree(profile, source), source)
    except Exception:  # noqa: BLE001 - a grammar that fails to load, or a shape a reader does not know
        return []


def pack(found: list[Fact]) -> bytes:
    if not found:
        return _NONE
    listed = [[fact.kind, fact.name, fact.line, fact.detail] for fact in found]
    return zlib.compress(json.dumps(listed, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogateescape"), 1)


def load(index: Index) -> list[Found]:
    """Every fact of the synced view that has been read, by name, then path, then line."""
    out: list[Found] = []
    rows = index.conn.execute(
        "SELECT a.blob_oid, a.family, a.facts FROM arch_facts a "
        "JOIN temp.view_blobs v ON v.blob_oid = a.blob_oid AND v.family = a.family "
        "WHERE a.version = ? AND length(a.facts) > 0", (FACTS_VERSION,))
    for oid, family, stored in rows:
        listed = json.loads(zlib.decompress(stored).decode("utf-8", "surrogateescape"))
        # The same bytes under a name that does not say what they are state nothing there.
        for path in (_stating(index, oid) if family == _TEXT else index.paths_for(oid, family)):
            out += [Found(kind, name, path, line, detail) for kind, name, line, detail in listed]
    out.sort(key=lambda found: (found.name, found.path, found.line, found.kind))
    return out


def select(found: list[Found], kind: str, like: str, k: int) -> tuple[list[Found], dict[str, int], bool]:
    """(the facts asked for, how many of each kind the view has, whether more matched than `k`)."""
    counts: dict[str, int] = {}
    for each in found:
        counts[each.kind] = counts.get(each.kind, 0) + 1
    counts = {name: counts[name] for name in KINDS if name in counts}
    if not kind and not like:
        return [], counts, False
    wanted = like.lower()
    listed = [each for each in found if (not kind or each.kind == kind) and wanted in each.name.lower()]
    return listed[:k], counts, len(listed) > k


def text(answer: Answer) -> str:
    """An answer as lines: what the view has of each kind, or the facts asked for."""
    if answer.kind or answer.like:
        lines = [f"{'' if answer.kind else each.kind + '  '}{each.name}  {each.path}:{each.line}  {each.detail}".rstrip()
                 for each in answer.facts]
        if not lines:
            lines = [f"no {answer.kind or 'fact'}" + (f' named like "{answer.like}"' if answer.like else "") + " is stated in this worktree"]
        if answer.truncated:
            lines.append("note: more match than are listed; narrow the name, or raise k")
    else:
        lines = [f"{kind}  {count}" for kind, count in answer.counts.items()] or ["no route, model, table, environment name, task or service is stated in this worktree"]
    if answer.pending:
        lines.append(f"note: {answer.pending} files are not indexed yet; the answer may be incomplete")
    if answer.unread:
        lines.append(f"note: {answer.unread} files are not read for facts yet; ask again to carry on")
    return "\n".join(lines)
