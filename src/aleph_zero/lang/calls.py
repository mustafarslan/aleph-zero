"""Call sites: where a file calls something, read from the same parse as its definitions.

A call is recorded by what is written, nothing more: the callee's own name (`save`), what
it is called on if anything (`self.store`), where the name stands, and the definition the
call sits in. Whether `save` is this file's `save`, an imported one, or one of forty
methods of that name is a question for the graph, which answers it per view and says how
sure it is (ADR-003). Calls with no name of their own (`get()(1)`, `(lambda: 1)()`) and
macro invocations are left out.

Which nodes are calls, and which field holds the callee, follows oxn's language profiles
(v0.1.2, MIT, same author).
"""
from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Node, Tree

from .definitions import Definition
from .profile import LanguageProfile

# Per language: the node kinds that are calls, and the field that holds what is called.
_SITES: dict[str, dict[str, str]] = {
    "python": {"call": "function"},
    "typescript": {"call_expression": "function", "new_expression": "constructor"},
    "tsx": {"call_expression": "function", "new_expression": "constructor"},
    "javascript": {"call_expression": "function", "new_expression": "constructor"},
    "go": {"call_expression": "function"},
    "rust": {"call_expression": "function"},
    "java": {"method_invocation": "name", "object_creation_expression": "type"},
    "cpp": {"call_expression": "function", "new_expression": "type"},
    "c": {"call_expression": "function"},
}
_NAME_FIELDS = ("attribute", "property", "field", "name")
_OWNER_FIELDS = ("object", "operand", "value", "argument", "path", "scope")
_WRAPPERS = {"generic_function": "function", "template_function": "name", "generic_type": None,
             "scoped_type_identifier": "name", "template_type": "name"}
_CALLABLE = frozenset({"function", "method"})


@dataclass(frozen=True)
class Call:
    name: str             # the callee's own name, as written
    receiver: str         # what it is called on, as written; '' for a plain call
    line: int             # 1-based, of the name
    column: int           # 0-based byte column of the name: where an index of occurrences looks
    caller: int | None    # index of the innermost function or method it sits in; None at file level


def calls(profile: LanguageProfile, tree: Tree, data: bytes, found: list[Definition]) -> list[Call]:
    """Every named call in the file, in source order."""
    sites = _SITES.get(profile.name)
    if not sites:
        return []
    callables = sorted((definition.end_byte - definition.start_byte, definition.start_byte, definition.end_byte, index)
                       for index, definition in enumerate(found) if definition.kind in _CALLABLE)
    out: list[Call] = []
    stack = [tree.root_node]
    while stack:  # explicit: expressions nest deeper than Python recurses
        node = stack.pop()
        stack.extend(reversed(node.children))
        field = sites.get(node.type)
        if field is None:
            continue
        callee = node.child_by_field_name(field)
        if callee is None:
            continue
        owner = node.child_by_field_name("object") if profile.name == "java" and node.type == "method_invocation" else None
        split = (callee, owner) if owner is not None or _is_name(callee) else _split(callee)
        if split is None:
            continue
        name, owner = split
        position = name.start_byte
        caller = next((index for _, start, end, index in callables if start <= position < end), None)
        out.append(Call(_text(name), _text(owner) if owner is not None else "", name.start_point[0] + 1,
                        name.start_point[1], caller))
    out.sort(key=lambda call: (call.line, call.column))
    return out


def _is_name(node: Node) -> bool:
    return node.child_count == 0 and "identifier" in node.type


def _split(callee: Node) -> tuple[Node, Node | None] | None:
    """The callee's own name and what it is called on, or None when it has no name."""
    node = callee
    for _ in range(4):  # `f::<T>()`, `ns::make<T>()`: the name is inside
        inner_field = _WRAPPERS.get(node.type, "")
        if inner_field == "":
            break
        inner = node.child_by_field_name(inner_field) if inner_field else (node.named_children[0] if node.named_children else None)
        if inner is None:
            return None
        node = inner
    if _is_name(node):
        return node, None
    for field in _NAME_FIELDS:
        name = node.child_by_field_name(field)
        if name is None:
            continue
        for _ in range(2):
            inner_field = _WRAPPERS.get(name.type, "")
            if not inner_field:
                break
            name = name.child_by_field_name(inner_field) or name
        if _is_name(name):
            owner = next((node.child_by_field_name(owner_field) for owner_field in _OWNER_FIELDS
                          if node.child_by_field_name(owner_field) is not None), None)
            return name, owner
        return None
    return None


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")
