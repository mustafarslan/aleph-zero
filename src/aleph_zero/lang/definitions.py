"""A parse tree's definitions: what is declared, what it is called, and where.

The walk covers everything outside callable bodies. A function nested in a function
is part of its parent, and a local variable is not a definition: both would flood a
by-name lookup with names nobody searches for.
"""
from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass

from tree_sitter import Node, Tree

from .profile import LanguageProfile

SIGNATURE_LIMIT = 300
DOC_LIMIT = 2000

_NAME_KINDS = frozenset({
    "identifier", "property_identifier", "private_property_identifier", "type_identifier",
    "field_identifier", "namespace_identifier", "destructor_name", "operator_name", "qualified_identifier",
    "nested_identifier", "nested_namespace_specifier", "operator_cast",
})
_LICENCE = re.compile(r"copyright|licen[cs]e|spdx", re.I)
_MEMBER_SCOPES = frozenset({"class", "interface", "enum", "impl"})
_ALL_CAPS = re.compile(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)*\Z")
_SPACE = re.compile(r"\s+")
_COMMENT_EDGE = re.compile(r"\A\s*(///?!?|//!?|/\*\*?!?|#+!?|\*+/?|--+)\s?|\s*\*+/\s*\Z")

# Fields under which a declaration holds the name it binds: `name` covers a TypeScript
# `variable_declarator` and a Go `var_spec`, `left` a Python assignment, `pattern` a Rust `let`.
_BINDING_FIELDS = ("name", "left", "pattern", "property")
_VALUE_FIELDS = ("value", "right")
_VALUE_WRAPPERS = frozenset({"expression_list", "parenthesized_expression"})
# Parents under which a callable is being used, not named.
_USE_KINDS = frozenset({
    "keyword_argument", "member_expression", "attribute", "field_expression",
    "selector_expression", "field_access",
})


@dataclass(frozen=True)
class Definition:
    kind: str          # function, method, class, interface, enum, type, namespace, constant, variable, field, macro
    name: str
    qualified: str     # dotted, within the file: TokenStore.get
    signature: str     # the declaration without its body, on one line
    doc: str           # its doc comment or docstring, markers removed
    start_line: int    # 1-based, including decorators and the doc comment above it
    end_line: int
    start_byte: int
    end_byte: int
    parent: int | None  # index of the enclosing definition in the same list


def definitions(profile: LanguageProfile, tree: Tree, data: bytes) -> list[Definition]:
    """Every definition in the file, in source order."""
    return _Walk(profile, data).run(tree.root_node)


@dataclass(frozen=True)
class _Scope:
    names: tuple[str, ...] = ()
    parent: int | None = None
    members: bool = False  # inside a type, where a callable is a method and a value a field


class _Walk:
    def __init__(self, profile: LanguageProfile, data: bytes) -> None:
        self.profile = profile
        self.data = data
        self.found: list[Definition] = []

    def run(self, root: Node) -> list[Definition]:
        # An explicit stack: expressions at module level can nest deeper than Python recurses.
        stack: list[tuple[Node, _Scope]] = [(child, _Scope()) for child in reversed(root.named_children)]
        while stack:
            node, scope = stack.pop()
            inside = self._consider(node, scope)
            if inside is not None:
                inner, inner_scope = inside
                stack.extend((child, inner_scope) for child in reversed(inner.named_children))
        return self.found

    def _consider(self, node: Node, scope: _Scope) -> tuple[Node, _Scope] | None:
        """Record `node` if it declares something. Returns what to look inside next."""
        profile = self.profile
        definition = self._unwrap(node)
        kind = definition.type
        if kind in profile.function_like:
            self._callable(node, definition, scope)
            return None
        if kind in profile.anonymous:
            if _bound_name(definition) is not None:
                self._callable(node, definition, scope)
            return None
        if kind in profile.types:
            return self._type(node, definition, scope)
        if kind in profile.values:
            return self._value(node, definition, scope)
        if kind in profile.pair_kinds:
            return self._pair(node, scope)
        return node, scope

    # ---- what each kind of node records ------------------------------------------------

    def _callable(self, node: Node, definition: Node, scope: _Scope) -> None:
        name_node = self._name_node(definition)
        if name_node is None:
            return
        outer = self._outer(node, definition)
        self._function(outer, definition, _text(name_node), scope)

    def _function(self, outer: Node, definition: Node, written: str, scope: _Scope) -> None:
        owners, name = _split_qualified(written)
        receiver = _receiver_type(definition, self.profile.receiver_field)
        if receiver:
            owners = (receiver,)
        kind = "method" if scope.members or owners else "function"
        self._add(kind, name, (*scope.names, *owners), scope.parent, outer, definition)

    def _type(self, node: Node, definition: Node, scope: _Scope) -> tuple[Node, _Scope] | None:
        profile = self.profile
        kind = profile.types[definition.type]
        if kind == "impl":
            owner = _implemented_type(definition, profile.implements_field)
            names = (*scope.names, owner) if owner else scope.names
            return definition, _Scope(names, scope.parent, members=True)
        body = definition.child_by_field_name(profile.body_field)
        if profile.declarator_chain and body is None and kind in ("class", "enum"):
            return None  # `class Later;` or `struct Pair p;`: a mention, not a definition
        for child in definition.named_children:
            kind = profile.type_refinement.get(child.type, kind)
        name_node = self._name_node(definition)
        if name_node is None:
            # Unnamed: what it holds belongs to whatever encloses it, but is still a member.
            return definition, _Scope(scope.names, scope.parent, members=scope.members or kind in _MEMBER_SCOPES)
        owners, name = _split_qualified(_text(name_node))
        index = self._add(kind, name, (*scope.names, *owners), scope.parent, self._outer(node, definition), definition)
        return definition, _Scope((*scope.names, *owners, name), index, members=kind in _MEMBER_SCOPES)

    def _pair(self, node: Node, scope: _Scope) -> tuple[Node, _Scope] | None:
        """`key: value` in an object literal. A function there is called by its key; an
        object there continues the name: `api.nested.deep`."""
        key = _sole_identifier(node.child_by_field_name("key"))
        value = node.child_by_field_name("value")
        if key is None or value is None:
            return node, scope
        if value.type in self.profile.anonymous:
            self._function(node, value, _text(key), scope)
            return None
        if value.type in self.profile.object_kinds and scope.members:
            return value, _Scope((*scope.names, _text(key)), scope.parent, members=True)
        return node, scope

    def _value(self, node: Node, definition: Node, scope: _Scope) -> tuple[Node, _Scope] | None:
        profile = self.profile
        chain = self._chain(definition)
        if len(chain) > 1:
            return self._chained(node, chain, scope)
        if self._holds_definition(definition):
            return definition, scope  # `const f = () => {}`: the function is the definition
        kind = profile.values[definition.type]
        outer = self._outer(node, definition)
        recorded: list[tuple[int, str, tuple[str, ...]]] = []
        for name_node, is_function in self._value_names(definition):
            written = _text(name_node)
            if is_function:
                self._function(outer, definition, written, scope)
                recorded.append((-1, "", ()))
            else:
                owners, name = _split_qualified(written)
                index = self._add(self._value_kind(kind, name, definition, scope), name, (*scope.names, *owners),
                                  scope.parent, outer, definition)
                recorded.append((index, name, owners))
        if not recorded:
            return definition, scope
        literal = self._value_node(definition)
        if len(recorded) == 1 and recorded[0][0] >= 0 and literal is not None and literal.type in profile.object_kinds:
            # `const api = { get() {} }`: what the object holds is reached through its name.
            index, name, owners = recorded[0]
            return literal, _Scope((*scope.names, *owners, name), index, members=True)
        return None

    def _chain(self, definition: Node) -> list[Node]:
        """`a = b = value` nests one assignment in another: all of them, outermost first."""
        chain = [definition]
        while True:
            inner = self._value_node(chain[-1])
            if inner is None or inner.type != definition.type:
                return chain
            chain.append(inner)

    def _chained(self, node: Node, chain: list[Node], scope: _Scope) -> None:
        """Every target of a chained assignment names the same value."""
        profile = self.profile
        outer = self._outer(node, chain[0])
        value = self._value_node(chain[-1])
        is_callable = value is not None and (value.type in profile.anonymous or value.type in profile.function_like)
        kind = profile.values[chain[0].type]
        for link in chain:
            for name_node, _ in self._value_names(link):
                written = _text(name_node)
                if is_callable:
                    self._function(outer, value, written, scope)
                else:
                    owners, name = _split_qualified(written)
                    self._add(self._value_kind(kind, name, chain[0], scope), name, (*scope.names, *owners),
                              scope.parent, outer, chain[0])
        return None

    def _value_node(self, definition: Node) -> Node | None:
        """What a declaration assigns, seen through parentheses."""
        for field_name in _VALUE_FIELDS:
            value = definition.child_by_field_name(field_name)
            while value is not None and value.type in _VALUE_WRAPPERS and value.named_child_count == 1:
                value = value.named_children[0]
            if value is not None:
                return value
        return None

    def _value_kind(self, kind: str, name: str, definition: Node, scope: _Scope) -> str:
        if kind in ("macro", "constant"):
            return kind
        if _ALL_CAPS.match(name) and len(name) > 1:
            return "constant"
        if scope.members:
            return "field"
        if kind == "variable" and self.profile.constant_tokens and (
                _tokens(definition) | _tokens(definition.parent)) & self.profile.constant_tokens:
            return "constant"
        return kind

    def _add(self, kind: str, name: str, scope_names: tuple[str, ...], parent: int | None,
             outer: Node, definition: Node) -> int:
        doc, first = self._doc(outer, definition)
        self.found.append(Definition(
            kind=kind, name=name, qualified=".".join((*scope_names, name)),
            signature=self._signature(outer, definition), doc=doc,
            start_line=first.start_point[0] + 1, end_line=_last_line(outer),
            start_byte=first.start_byte, end_byte=outer.end_byte, parent=parent,
        ))
        return len(self.found) - 1

    # ---- reading one node ----------------------------------------------------------------

    def _unwrap(self, node: Node) -> Node:
        """Follow wrapper nodes down to what they wrap. Unchanged when it is not a wrapper."""
        profile = self.profile
        current = node
        for _ in range(8):
            wrapper = profile.wrappers.get(current.type)
            if wrapper is None:
                break
            if wrapper.field_name:
                inner = current.child_by_field_name(wrapper.field_name)
            else:
                inner = next((child for child in current.named_children
                              if profile.is_definition(child.type) or child.type in profile.values
                              or child.type in profile.wrappers), None)
            if inner is None:
                break
            current = inner
        return current

    def _outer(self, node: Node, definition: Node) -> Node:
        """The node whose lines belong to the definition: its wrapper, or the statement
        that holds nothing else (`type X struct{}` around a Go type_spec)."""
        outer = node
        if outer is definition and definition.type in self.profile.anonymous:
            bound = _bound_name(definition)
            while bound is not None and outer.parent is not None and outer.start_byte > bound.start_byte:
                outer = outer.parent  # the binding `name = ...` is part of the declaration
        while True:
            parent = outer.parent
            if parent is None or parent.parent is None:
                return outer
            if parent.type in self.profile.wrappers or (
                    parent.start_byte == outer.start_byte and parent.end_byte == outer.end_byte):
                outer = parent
            elif parent.type in self.profile.holders and sum(
                    child.type not in self.profile.comment_kinds for child in parent.named_children) == 1:
                outer = parent
            else:
                return outer

    def _name_node(self, definition: Node) -> Node | None:
        if self.profile.declarator_chain:
            found, _ = _drill(definition)
            return found
        if definition.type in self.profile.anonymous:
            # `var visible = function inner() {}` is called as `visible`; `inner` exists only inside it.
            bound = _bound_name(definition)
            if bound is not None:
                return bound
        named = definition.child_by_field_name(self.profile.name_field)
        if named is not None and named.type in _NAME_KINDS:
            return named
        return _bound_name(definition)

    def _value_names(self, definition: Node) -> list[tuple[Node, bool]]:
        """The names a value node declares, each with whether it turned out to be a function
        prototype (C and C++ declare those with the same node kind as variables)."""
        if self.profile.declarator_chain:
            targets = definition.children_by_field_name("declarator")
            found = [_drill(target, itself=True) for target in targets] if targets else [_drill(definition)]
            return [(node, is_function) for node, is_function in found if node is not None]
        named = definition.children_by_field_name("name") or definition.children_by_field_name("property")
        if not named:
            named = [child.child_by_field_name("name") or child
                     for child in definition.children_by_field_name("declarator")]
        if not named:
            left = definition.child_by_field_name("left")
            named = [left] if left is not None else []
        return [(node, False) for node in named if node is not None and node.type in _NAME_KINDS]

    def _holds_definition(self, definition: Node) -> bool:
        """True when the value being declared is itself a callable or a type."""
        for field_name in _VALUE_FIELDS:
            value = definition.child_by_field_name(field_name)
            while value is not None and value.type in _VALUE_WRAPPERS and value.named_child_count == 1:
                value = value.named_children[0]
            if value is not None and self.profile.is_definition(value.type):
                return True
        return False

    def _signature(self, outer: Node, definition: Node) -> str:
        body = definition.child_by_field_name(self.profile.body_field)
        if body is not None and body.start_byte > outer.start_byte:
            written = self.data[outer.start_byte:body.start_byte]
        else:
            written = self.data[outer.start_byte:outer.end_byte]
            if definition.type not in self.profile.values:
                written = written.split(b"{", 1)[0]
            written = written.split(b"\n", 1)[0] if definition.type in self.profile.values else written
        text = _SPACE.sub(" ", written.decode("utf-8", "replace")).strip()
        for ending in ("{", "=>", ":", ";", ","):
            text = text.removesuffix(ending).rstrip()
        return text[:SIGNATURE_LIMIT]

    def _doc(self, outer: Node, definition: Node) -> tuple[str, Node]:
        """The documentation of a definition, and the first node of its range: the
        earliest comment directly above it, or the definition itself."""
        profile = self.profile
        comments: list[Node] = []
        first = outer
        sibling = outer.prev_named_sibling
        while sibling is not None and sibling.type in profile.comment_kinds:
            if _last_line(sibling) < first.start_point[0]:  # a blank line in between
                break
            before = sibling.prev_sibling
            if before is not None and before.end_point[0] == sibling.start_point[0] \
                    and before.type not in profile.comment_kinds:
                break  # a trailing comment of the line above
            comments.append(sibling)
            first = sibling
            sibling = sibling.prev_named_sibling
        if profile.docstrings:
            body = definition.child_by_field_name(profile.body_field)
            opening = body.named_children[0] if body is not None and body.named_child_count else None
            if opening is not None and opening.type == "expression_statement" and opening.named_child_count:
                opening = opening.named_children[0]
            if opening is not None and opening.type in profile.string_kinds:
                return _docstring(_text(opening)), first
        lines = [line for comment in reversed(comments) for line in _text(comment).splitlines()]
        if comments and first.start_byte == 0 and _LICENCE.search("\n".join(lines)):
            return "", outer  # the file's licence header, not this definition's documentation
        return _comment_text(lines), first


# ---- helpers that need no profile -------------------------------------------------------------


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace") if node.text else ""


def _last_line(node: Node) -> int:
    """1-based last line. A node ending at column 0 ended with the line before."""
    row, column = node.end_point
    return row if column == 0 and row > node.start_point[0] else row + 1


def _split_qualified(written: str) -> tuple[tuple[str, ...], str]:
    """`TokenStore::Get` -> (('TokenStore',), 'Get'). Template arguments are dropped."""
    if written.startswith("operator"):
        return (), _SPACE.sub(" ", written.split("(")[0]).strip()  # `operator bool`, `operator==`
    if "::" not in written and "." not in written:
        return (), written
    parts = [part.split("<")[0].strip() for part in re.split(r"::|\.", written)]
    parts = [part for part in parts if part]
    return tuple(parts[:-1]), parts[-1] if parts else written


def _tokens(node: Node | None) -> set[str]:
    """Token kinds on a node and inside its small modifier children."""
    if node is None:
        return set()
    found: set[str] = set()
    for child in node.children:
        found.add(child.type)
        if child.is_named and child.child_count <= 4:
            found.update(grandchild.type for grandchild in child.children)
            if child.child_count == 0:
                found.add(_text(child))
    return found


def _drill(node: Node, itself: bool = False) -> tuple[Node | None, bool]:
    """Follow `declarator` (then `name`) fields down to the name a C or C++ declaration
    binds. Also says whether the chain passed a function declarator. With `itself`, the
    starting node may be the name."""
    current, is_function = node, False
    for _ in range(12):
        if current.type in ("function_declarator", "operator_cast"):
            is_function = True  # a conversion operator carries its own parameter list
        elif current.type == "parenthesized_declarator":
            is_function = False  # `void (*callback)(int)` declares a pointer, not a function
        if current.type in _NAME_KINDS and (itself or current is not node):
            if current.type == "qualified_identifier" and b"::" not in (current.text or b""):
                # `EXPORT_MACRO type name(...)`: an unknown macro makes the parser read
                # "type name" as a qualified name. The real name is its last part.
                inner = current.child_by_field_name("name")
                if inner is not None:
                    current = inner
                    continue
            return current, is_function
        following = current.child_by_field_name("declarator") or current.child_by_field_name("name")
        if following is None:
            # `T& f()`, `(*f)(int)`: reference and parenthesised declarators hold the next
            # one as a plain child.
            following = next((child for child in current.named_children
                              if child.type.endswith("_declarator") or child.type in _NAME_KINDS), None)
        if following is None:
            return None, is_function
        current = following
    return None, is_function


def _receiver_type(node: Node, field_name: str) -> str | None:
    """The type a callable is a method of, where it is written outside any class body.
    Pointer and value receivers name the same type: `(c *Counter)` and `(c Counter)`."""
    receiver = node.child_by_field_name(field_name) if field_name else None
    parts = _text(receiver).strip("()").strip().split() if receiver is not None else []
    return (parts[-1].lstrip("*").split("[")[0] or None) if parts else None


def _implemented_type(node: Node, field_name: str) -> str:
    """The type a member-holding block belongs to: `impl Display for Service` -> Service.
    Generic arguments are dropped, so `impl<R> Reader<R>` and `struct Reader` are one type."""
    named = node.child_by_field_name(field_name) if field_name else None
    return _text(named).split("<")[0].strip() if named is not None else ""


def _bound_name(node: Node) -> Node | None:
    """The identifier a callable was assigned to, when it has no name of its own.

    Only the immediate parent, and only when it yields a plain identifier:
    `const [a, b] = ...` destructures and `obj.method = () => {}` binds an attribute,
    and neither is a name this callable can be called by.
    """
    parent = node.parent
    if parent is not None and parent.type in _VALUE_WRAPPERS and parent.named_child_count == 1:
        parent = parent.parent
    if parent is None or parent.type in _USE_KINDS:
        return None
    for field_name in _BINDING_FIELDS:
        bound = _sole_identifier(parent.child_by_field_name(field_name))
        if bound is not None and bound != node:
            return bound
    return None


def _sole_identifier(bound: Node | None) -> Node | None:
    """A name, seen through a wrapper holding exactly one. Go wraps the name of
    `g := func() {}` in an `expression_list` exactly as it wraps the value."""
    if bound is None:
        return None
    if bound.type not in _NAME_KINDS and bound.named_child_count == 1:
        bound = bound.named_children[0]
    return bound if bound.type in ("identifier", "property_identifier") else None


def _docstring(literal: str) -> str:
    text = literal.lstrip("rRbBuUfF")
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote) and text.endswith(quote) and len(text) >= 2 * len(quote):
            text = text[len(quote):-len(quote)]
            break
    first, _, rest = text.partition("\n")
    return (first.strip() + ("\n" + textwrap.dedent(rest) if rest.strip() else "")).strip()[:DOC_LIMIT]


def _comment_text(lines: list[str]) -> str:
    cleaned = []
    for line in lines:
        stripped = line
        for _ in range(2):  # an opening marker and a closing one can share a line
            stripped = _COMMENT_EDGE.sub("", stripped)
        cleaned.append(stripped.rstrip())
    return "\n".join(cleaned).strip()[:DOC_LIMIT]
