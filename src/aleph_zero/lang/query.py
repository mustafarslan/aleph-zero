"""Finding nodes of given kinds without visiting every node from Python.

A walk of a whole parse tree in Python costs a few hundred nanoseconds per node, and a
large repository has millions of nodes: three such walks more than doubled the time to
index Django. The parser's own query engine does the walk in C and hands back only the
nodes asked for.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from tree_sitter import Node, Query, QueryCursor, QueryError, Tree

_QUERIES: dict[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], Query | None] = {}


def _query(family: str, tree: Tree, roles: tuple[tuple[str, tuple[str, ...]], ...]) -> Query | None:
    key = (family, roles)
    if key not in _QUERIES:
        patterns = []
        for role, kinds in roles:
            for kind in kinds:
                try:
                    Query(tree.language, f"({kind}) @{role}")     # a kind this grammar does not have is left out
                except QueryError:
                    continue
                patterns.append(f"({kind}) @{role}")
        _QUERIES[key] = Query(tree.language, "\n".join(patterns)) if patterns else None
    return _QUERIES[key]


def by_role(family: str, tree: Tree, roles: Mapping[str, Iterable[str]], within: Node | None = None) -> dict[str, list[Node]]:
    """For each role, the nodes of its kinds under `within` (the whole tree by default)."""
    query = _query(family, tree, tuple((role, tuple(sorted(kinds))) for role, kinds in sorted(roles.items())))
    if query is None:
        return {}
    return QueryCursor(query).captures(within if within is not None else tree.root_node)


def find(family: str, tree: Tree, kinds: Iterable[str], within: Node | None = None) -> list[Node]:
    """Every node of one of `kinds`, in source order, an outer node before what it contains."""
    found = by_role(family, tree, {"n": kinds}, within).get("n", [])
    return sorted(found, key=lambda node: (node.start_byte, -node.end_byte))
