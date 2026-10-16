"""Architectural facts of a view: the routes it serves, its models and tables, the environment
names it reads, its tasks and services, and where its routers are mounted (M8).

What one file states does not depend on any other file, so it is kept per blob, like the code
graph's facts, and like them it is read when first asked for: an index that is only searched
pays nothing. An answer is the facts of the files the view holds now, each with its path and line.
"""
from __future__ import annotations

import json
import re
import time
import zlib
from dataclasses import dataclass, field

import apsw

from . import architecture, filters, gitio, lang, symbols
from .indexer import Index
from .lang import parse
from .lang.facts import Fact, facts, states_facts
from .lang.facts import text_facts as _named_text_facts
from .store.db import transaction
from .view import View

FACTS_VERSION = 5           # bump when what the readers extract changes: every row is then read again
PREPARE_BATCH = 64          # blobs read between two commits
KINDS = ("route", "mount", "model", "table", "env", "task", "queue", "service", "rpc", "layer", "contract")
_CODE = sorted({"typescript", "tsx", "javascript", "python", "go", "rust", "java"} & set(lang.PROFILES))
_TEXT = "text"              # the family of a Prisma schema, an example environment file, a .proto, an oxn.yaml
_READ = "(" + ",".join("?" * (len(_CODE) + 1)) + ")"
_PENDING = (
    "SELECT v.blob_oid, v.family FROM temp.view_blobs v "
    "JOIN blobs b ON b.blob_oid = v.blob_oid AND b.family = v.family "
    "LEFT JOIN arch_facts a ON a.blob_oid = v.blob_oid AND a.family = v.family AND a.version = ? "
    f"WHERE a.blob_oid IS NULL AND v.family IN {_READ}"
)
_DETAIL_LIMIT = 400
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
    return [path for path in index.paths_for(oid, _TEXT) if path == architecture.FILE or states_facts(path)]


def text_facts(path: str, text: str) -> list[Fact]:
    """The facts of a file that is not source code: the layers and contracts of an `oxn.yaml` at the
    root, or what the language package reads by a file's name (a schema, an example environment file)."""
    if path != architecture.FILE:
        return _named_text_facts(path, text)
    layers, contracts = architecture.read(text)
    return ([Fact("layer", layer.name, layer.line, ", ".join(layer.patterns)[:_DETAIL_LIMIT]) for layer in layers]
            + [Fact("contract", contract.name, contract.line,
                    (contract.kind + (": " + " > ".join(contract.order) if contract.order else ""))[:_DETAIL_LIMIT])
               for contract in contracts])


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
    found = extract(family, path, text)
    return None if found is None else pack(found)


def extract(family: str, path: str, text: str) -> list[Fact] | None:
    """The facts of one file; None when a reader failed on it. Nothing is recorded then: a row saying "states
    nothing" would be believed for as long as the readers' version stands. The file stays unread, and is tried again."""
    text = text.replace("\r\n", "\n")
    try:
        if family == _TEXT:
            return text_facts(path, text)
        profile = lang.PROFILES[family]
        source = text.encode("utf-8", "surrogateescape")
        return facts(profile, parse.tree(profile, source), source)
    except Exception:  # noqa: BLE001 - a grammar that fails to load, or a shape a reader does not know
        return None


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


# ---- what a request names -------------------------------------------------------------------------
# A prompt, an error or a grep pattern that names a route or an environment name is answered with where the code states it:
# what the plugin hands the agent without being asked (DEFERRED 0-graph, part 4). Only what the code states: never a
# document, an example environment file, an OpenAPI or compose file, or a test; an environment name only declared is not read.

MENTIONED_LIMIT = 5          # names read from one text
_PER_NAME = 3                # routes, and mounts, kept for one name
_METHOD_PATH = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(?:https?://[^/\s]+)?(/[^\s`'\"),;\]?#]*)")
_URL_PATH = re.compile(r"\bhttps?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|[\w.-]+:\d+|[\w-]+\.(?:example|test|local)\b[\w.]*)"
                       r"(?::\d+)?(/[^\s`'\"),;\]?#]*)")
_BARE_PATH = re.compile(r"(?:^|(?<=[\s`'\"(\[]))(/[\w.~{}<>:*-]+(?:/[\w.~{}<>:*-]*)+)")
_ENV_MENTION = re.compile(r"(?<![\w$.-])([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?![\w(-])")
_FILE_ROOTS = frozenset({"usr", "etc", "tmp", "var", "home", "users", "opt", "bin", "sbin", "dev", "proc", "private", "library",
                         "system", "applications", "volumes", "mnt", "srv", "root", "nix", "snap", "lib", "lib64", "run"})
_A_FILE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,5}$")
_PARAMETER = re.compile(r"^(\{.*\}|:.+|<.+>|\*.*|\d+|[0-9a-f]{8}-[0-9a-f-]{27,}|\$\{.*\})$", re.IGNORECASE)
NEVER_AN_ANSWER = re.compile(r"(^|/)(docs?|documentation)/|\.(md|mdx|rst|txt|adoc)$|(^|/)\.env|openapi|swagger|"
                             r"(^|/)(docker-)?compose[^/]*\.ya?ml$|(^|/)Dockerfile|"
                             r"(^|/)(tests?|__tests__|spec|e2e)/|_test\.(go|py)$|(^|/)test_[^/]*\.py$|\.(test|spec)\.[cm]?[jt]sx?$|"
                             r"Tests?\.(java|kt)$", re.IGNORECASE)


@dataclass(frozen=True)
class Mention:
    kind: str                    # 'route' or 'env'
    name: str                    # 'GET /client/{id}', '/client/{id}' (no method said) or 'DB_URL'


def _segments(path: str) -> list[str]:
    """A path's segments, a parameter in any spelling ({id}, :id, <int:id>, *, a number, a uuid) as '*'."""
    return ["*" if _PARAMETER.match(part) else part.lower() for part in path.split("?")[0].strip("/").split("/") if part]


def mentioned(text: str) -> list[Mention]:
    """The routes and environment names a text names, in the order written, at most MENTIONED_LIMIT. A route: a method and a
    path, a path in a URL to a local or example host, or a path of two segments or more that is not a file's."""
    found: list[tuple[int, Mention]] = []
    covered: list[tuple[int, int]] = []
    for match in _METHOD_PATH.finditer(text):
        path = match.group(2).rstrip(".:")
        if _segments(path):
            found.append((match.start(), Mention("route", f"{match.group(1)} {path}")))
            covered.append(match.span(2))
    for pattern in (_URL_PATH, _BARE_PATH):
        for match in pattern.finditer(text):
            start, end = match.span(1)
            if any(start < right and left < end for left, right in covered):
                continue
            covered.append((start, end))
            path = match.group(1).rstrip(".:")
            parts = [part for part in path.strip("/").split("/") if part]
            if not parts or _A_FILE.search(parts[-1]) or (pattern is _BARE_PATH and (len(parts) < 2 or parts[0].lower() in _FILE_ROOTS)):
                continue
            found.append((start, Mention("route", path)))
    for match in _ENV_MENTION.finditer(text):
        found.append((match.start(), Mention("env", match.group(1))))
    out: list[Mention] = []
    for _, mention in sorted(found, key=lambda pair: pair[0]):
        if mention not in out:
            out.append(mention)
    return out[:MENTIONED_LIMIT]


def _route_match(asked: list[str], stated: list[str]) -> int:
    """How many segments two route paths agree on, read from their ends (a router's mount prefix is not in a route's path, and
    a question may leave out a prefix the code has); 0 unless every segment of the shorter agrees and one of them is a word."""
    shorter = min(len(asked), len(stated))
    if shorter == 0:
        return 0
    pairs = list(zip(reversed(asked), reversed(stated)))[:shorter]
    if any(a != s and a != "*" and s != "*" for a, s in pairs):
        return 0
    words = sum(1 for a, s in pairs if a == s and a != "*")
    return shorter if words else 0


def matching(found: list[Found], mentions: list[Mention]) -> list[Found]:
    """The facts of source code that answer the mentions: an environment name read or set by exactly that name; the routes
    whose path agrees with the one named, longest agreement first (the method too, when one was named); the mounts whose
    prefix the path starts with."""
    usable = [each for each in found if not NEVER_AN_ANSWER.search(each.path)]
    out: list[Found] = []
    for mention in mentions:
        if mention.kind == "env":
            out += [each for each in usable if each.kind == "env" and each.name == mention.name and each.detail != "declared"][:_PER_NAME]
            continue
        method, _, path = mention.name.rpartition(" ")
        asked = _segments(path)
        routes = []
        for each in usable:
            if each.kind != "route":
                continue
            stated_method, _, stated_path = each.name.partition(" ")
            if method and stated_method not in (method, "ANY"):
                continue
            agreed = _route_match(asked, _segments(stated_path))
            if agreed:
                routes.append((-agreed, each))
        routes.sort(key=lambda pair: pair[0])
        best = routes[0][0] if routes else 0
        out += [each for agreed, each in routes if agreed == best][:_PER_NAME]
        mounts = []
        for each in usable:
            prefix = _segments(each.name) if each.kind == "mount" else []
            if prefix and len(prefix) <= len(asked) and all(p == a or "*" in (p, a) for p, a in zip(prefix, asked)) \
                    and any(p == a != "*" for p, a in zip(prefix, asked)):
                mounts.append((-len(prefix), each))
        mounts.sort(key=lambda pair: pair[0])
        out += [each for _, each in mounts[:_PER_NAME - 1]]
    unique: list[Found] = []
    for each in out:
        if each not in unique:
            unique.append(each)
    return unique


def handlers(index: Index, view: View, routes: list[Found]) -> list[Found]:
    """Where the handler a route names is defined (`clientHandler.DeleteClient`: the definition of DeleteClient), as a fact of
    kind 'handler' named by the route. Left out when the name is defined in more than three places outside tests."""
    out: list[Found] = []
    for route in routes:
        framework, _, handler = route.detail.partition(" ")
        name = handler.rsplit(".", 1)[-1]
        if route.kind != "route" or not name or not name.isidentifier():
            continue
        defined = [hit for hit in symbols.lookup(index, view, name, limit=20, nested=False)
                   if hit.name == name and not NEVER_AN_ANSWER.search(hit.path)]
        if 0 < len(defined) <= 3:
            out += [Found("handler", route.name, hit.path, hit.line, handler) for hit in defined]
    return out


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
