"""Looking a definition up by name, in the current view."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .indexer import Index, symbol_key
from .view import View

# What someone asking for a name most likely means, most likely first.
_KIND_RANK = {
    "class": 0, "interface": 0, "enum": 0, "type": 0, "function": 0, "method": 1,
    "constant": 2, "macro": 2, "variable": 3, "namespace": 4, "field": 5,
}
_SEPARATOR = re.compile(r"::|\.")
# A definition in test code is rarely the one meant when the same name exists outside it.
_TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__|testdata)/|(^|/)test_[^/]*$|[._-](test|spec)\.[^/.]+$")
_FETCH = 5000  # rows read for one name before ranking; a name defined more often is cut here


@dataclass(frozen=True)
class SymbolHit:
    path: str
    line: int
    kind: str
    name: str
    qualified: str
    signature: str
    chunk_id: int


def rows(index: Index, name: str, scoped: bool = False) -> list[tuple]:
    """Definitions in the view whose name is `name`, any case: (blob, family, name, qualified,
    kind, signature, line, chunk id), best first. A dotted or `::` name must match the end
    of the qualified name. With `scoped`, only chunks in temp.scope_chunks."""
    parts = [part for part in _SEPARATOR.split(name.strip()) if part]
    if not parts:
        return []
    wanted = ".".join(parts).lower()
    last = symbol_key(parts[-1])
    found = index.conn.execute(
        "SELECT s.blob_oid, s.family, s.name, s.qualified, s.kind, s.signature, s.line, bc.chunk_id "
        "FROM symbols s JOIN temp.view_blobs v ON v.blob_oid = s.blob_oid AND v.family = s.family "
        "JOIN blob_chunks bc ON bc.blob_oid = s.blob_oid AND bc.family = s.family AND bc.ord = s.chunk_ord "
        + ("JOIN temp.scope_chunks sc ON sc.chunk_id = bc.chunk_id " if scoped else "")
        + "WHERE s.name_key = ? LIMIT ?",
        (last, _FETCH),
    ).fetchall()
    if len(parts) > 1:
        found = [row for row in found
                 if row[3].lower().replace("#", "") == wanted or row[3].lower().replace("#", "").endswith("." + wanted)]
    exact = parts[-1].lstrip("#")

    def rank(row: tuple) -> tuple:
        paths = index.paths_for(row[0], row[1])
        return (row[2].lstrip("#") != exact, _KIND_RANK.get(row[4], 9), all(_TEST_PATH.search(path) for path in paths),
                row[3].count("."), paths[0] if paths else "", row[6])

    found.sort(key=rank)
    return found


def lookup(index: Index, view: View, name: str, limit: int = 20) -> list[SymbolHit]:
    """Where `name` is defined in the view, best first: the exact spelling before other
    cases, types and functions before fields."""
    if not index.serves(view):
        raise ValueError("this view is not the one the index was last synced to; call index.sync(view) first")
    hits: list[SymbolHit] = []
    for oid, family, found_name, qualified, kind, signature, line, chunk_id in rows(index, name):
        for path in index.paths_for(oid, family):
            hits.append(SymbolHit(path, line, kind, found_name, qualified, signature, chunk_id))
            if len(hits) >= limit:
                return hits
    return hits
