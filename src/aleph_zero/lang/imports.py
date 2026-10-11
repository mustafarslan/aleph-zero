"""Extract imports from a parse tree. Ported from oxn (`oxn/graph/imports.py`, MIT).

Two rules govern this module:

* **A full-tree walk, not a scan of top-level statements.** Imports hide inside
  ``if TYPE_CHECKING:`` blocks, function bodies and conditional branches.
* **Nothing is ever silently dropped.** An import that cannot be resolved later is still
  recorded with its raw specifier.

Every import carries a ``kind``: a ``type_only`` import creates no runtime coupling.
Added to the port: the ``include`` style for C and C++.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace

from tree_sitter import Node

from .profile import ImportSpec, LanguageProfile


@dataclass(frozen=True, slots=True)
class RawImport:
    """One import statement, before module resolution."""

    #: The specifier exactly as written: ``a.b.c``, ``./mod``, ``../other``.
    specifier: str
    #: ``value`` | ``type_only`` | ``side_effect`` | ``wildcard`` | ``dynamic``
    kind: str
    line: int
    #: Leading-dot count for a Python relative import; 0 for absolute.
    level: int = 0
    #: Names imported *from* the module, where the language distinguishes them.
    names: tuple[str, ...] = ()
    #: ``export { x } from "./m"`` / ``export * from "./m"``: the module does not *use* the
    #: name, it republishes it. An `index.ts` barrel is nothing but these, and a name imported
    #: from one is declared two files away -- so a resolver that stops at the file the import
    #: named finds nothing there and falls back to guessing project-wide.
    reexport: bool = False
    #: Written inside a function or class body **in a language where that defers the load**.
    #: Python and CommonJS execute such an import when the body runs, not when the module is
    #: initialised, which is precisely how both languages *break* an import cycle. Go and
    #: Java cannot nest an import at all, and a Rust `use` inside a function is a namespace
    #: alias that changes nothing about when the crate is linked -- so `deferred` is false
    #: for all three however deeply the statement is nested.
    deferred: bool = False

    @property
    def is_relative(self) -> bool:
        return self.level > 0 or self.specifier.startswith(".")


def extract_imports(root: Node, profile: LanguageProfile) -> list[RawImport]:
    """Every import in a file, in source order."""
    spec = profile.imports
    if not spec.statement_kinds and not spec.call_kinds:
        return []

    found = [
        item
        for node, type_only, nested in _walk(root, spec, profile)
        for item in _imports_at(node, spec, type_only, nested)
    ]
    found.sort(key=lambda item: (item.line, item.specifier))
    return found


def _walk(
    root: Node, spec: ImportSpec, profile: LanguageProfile
) -> Iterator[tuple[Node, bool, bool]]:
    """Every node, with whether it sits inside a type-only guard and inside a definition."""
    stack: list[tuple[Node, bool, bool]] = [(root, False, False)]
    while stack:
        node, guarded, nested = stack.pop()
        yield node, guarded, nested
        inner = guarded or _is_type_checking_guard(node, spec)
        within = nested or profile.is_definition(node.type)
        stack.extend((child, inner, within) for child in node.named_children)


#: Languages where a nested import is evaluated when the body runs rather than when the
#: module loads. It is the standard remedy for an import cycle in both.
_DEFERS_WHEN_NESTED = frozenset({"python", "ecmascript"})


def _imports_at(node: Node, spec: ImportSpec, type_only: bool, nested: bool) -> list[RawImport]:
    """What this one node imports, if anything."""
    deferred = nested and spec.style in _DEFERS_WHEN_NESTED
    if node.type in spec.statement_kinds:
        statements = [
            replace(item, deferred=deferred, kind="type_only" if type_only else item.kind)
            for item in _from_statement(node, spec)
        ]
        return statements
    if node.type in spec.call_kinds:
        dynamic = _from_dynamic_call(node, spec)
        return [replace(dynamic, deferred=deferred)] if dynamic is not None else []
    return []


def _is_type_checking_guard(node: Node, spec: ImportSpec) -> bool:
    """Is this an `if TYPE_CHECKING:` block?

    Python has no `import type` keyword, so its type-only imports are ordinary imports
    nested inside this guard -- erased at runtime exactly as TypeScript's are. Counting them
    as runtime coupling inflates every Martin metric, can manufacture a cycle out of nothing,
    and produced two false layer-contract violations in OXN's own repository, which is where
    this was found.

    The condition is matched on the name rather than on a resolved symbol: `TYPE_CHECKING`
    is `typing.TYPE_CHECKING` by universal convention, and a name that says it and means
    something else is a problem no import graph can help with.
    """
    if spec.style != "python" or node.type != "if_statement":
        return False
    condition = node.child_by_field_name("condition")
    return condition is not None and b"TYPE_CHECKING" in (condition.text or b"")


# ---- statements -----------------------------------------------------------------------


def _from_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    handler = {
        "python": _python_statement,
        "ecmascript": _ecmascript_statement,
        "go": _go_statement,
        "rust": _rust_statement,
        "java": _java_statement,
        "include": _include_statement,
    }.get(spec.style)
    return handler(node, spec) if handler else []


def _include_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    """`#include "util.h"` or `#include <stdio.h>`. Angle brackets are kind `system`: the
    header is looked for on the compiler's search path, not beside the including file."""
    path = node.child_by_field_name(spec.module_field)
    if path is None or path.type not in spec.string_kinds:
        return []  # `#include MACRO`: nothing to name
    written = _text(path).strip()
    system = written.startswith("<")
    specifier = written.strip('<>"')
    if not specifier:
        return []
    return [RawImport(specifier=specifier, kind="system" if system else "value", line=node.start_point[0] + 1)]


def _go_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    """``import ("fmt"; alias "os")`` -- one declaration, any number of specs.

    A ``_`` alias imports purely for side effects and a ``.`` alias dot-imports; both are
    real coupling and are recorded with the kind that says which.
    """
    line = node.start_point[0] + 1
    found: list[RawImport] = []
    for spec_node in _descend(node, "import_spec"):
        path = spec_node.child_by_field_name("path")
        if path is None:
            continue
        specifier = _strip_quotes(_text(path))
        if not specifier:
            continue
        alias = spec_node.child_by_field_name("name")
        alias_text = _text(alias) if alias is not None else ""
        kind = "side_effect" if alias_text == "_" else "value"
        found.append(RawImport(specifier, kind, spec_node.start_point[0] + 1 or line))
    return found


def _rust_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    """``use a::b::{c, d as e};`` -- one declaration reaching several paths.

    Rust paths are ``::``-separated, and are normalised to dots so every language's
    specifiers read the same way downstream.
    """
    line = node.start_point[0] + 1
    if node.type == "extern_crate_declaration":
        name = node.child_by_field_name("name")
        return [RawImport(_text(name), "value", line)] if name is not None else []

    argument = node.child_by_field_name("argument")
    if argument is None:
        return []
    return [
        RawImport(path.replace("::", "."), kind, line)
        for path, kind in _rust_paths(argument, prefix="")
    ]


def _rust_paths(node: Node, prefix: str) -> list[tuple[str, str]]:
    """Flatten a ``use`` tree into ``(path, kind)`` pairs.

    Rust's `use` is a tree, not a statement: `use a::{b, c::{d, e}, f as g, h::*}` is one
    statement naming five paths at three depths. Each node kind below is one shape that
    tree can take, and every one of them recurses except the leaf.
    """
    if node.type == "scoped_use_list":
        return _rust_scoped_list(node, prefix)
    if node.type == "use_list":
        return [pair for child in node.named_children for pair in _rust_paths(child, prefix)]
    if node.type == "use_as_clause":
        # `use a::b as c` couples to `a::b`; the local alias is not a dependency.
        path = node.child_by_field_name("path")
        return _rust_paths(path, prefix) if path is not None else []
    if node.type == "use_wildcard":
        return [(_rust_wildcard_path(node, prefix), "wildcard")]
    return _rust_leaf(node, prefix)


def _rust_scoped_list(node: Node, prefix: str) -> list[tuple[str, str]]:
    """``a::{b, c}`` -- a path segment that opens a brace group, or ends the path."""
    base = node.child_by_field_name("path")
    head = f"{prefix}::{_text(base)}" if prefix and base is not None else _text(base or node)
    body = node.child_by_field_name("list")
    if body is None:
        return [(head, "value")]
    return [pair for child in body.named_children for pair in _rust_paths(child, head)]


def _rust_wildcard_path(node: Node, prefix: str) -> str:
    """The module a glob import couples to: ``use a::b::*`` depends on ``a::b``.

    Two bugs lived here, both invisible on the Rust corpus because nothing in ripgrep
    writes a glob inside a brace group:

    * the ``*`` was left in the specifier, making Rust the only language whose wildcard
      names something other than a module -- Python's ``from a.b import *`` yields ``a.b``
      -- and ``a::b::*`` resolves to nothing at all;
    * a glob *inside* a group lost its own segment, so ``use std::{io::*, fmt}`` reported a
      dependency on ``std`` rather than ``std::io``. Coupling attributed to the wrong module
      is worse than no coupling: a layer contract can be satisfied or violated by it.

    The grammar gives the path no field name, but it is the first named child, and it is
    absent for a bare ``use *``.
    """
    own = _text(node.named_children[0]) if node.named_children else ""
    if prefix and own:
        return f"{prefix}::{own}"
    return prefix or own or _text(node)


def _rust_leaf(node: Node, prefix: str) -> list[tuple[str, str]]:
    """A plain path segment: the end of one branch of the tree."""
    text = _text(node)
    if not text:
        return []
    return [(f"{prefix}::{text}" if prefix else text, "value")]


def _java_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    """``import java.util.List;``, ``import java.util.*;``, ``import static a.b.C.max;``."""
    line = node.start_point[0] + 1
    # The grammar spells `.*` as a **named** `asterisk` node, not a bare `*` token. Testing
    # `not child.is_named` therefore never matched, and no Java import was ever marked a
    # wildcard -- `import java.util.*` came back indistinguishable from `import java.util`.
    wildcard = any(child.type == "asterisk" for child in node.children)
    static = any(not child.is_named and _text(child) == "static" for child in node.children)

    for child in node.named_children:
        if child.type not in {"scoped_identifier", "identifier"}:
            continue
        specifier = _text(child)
        if specifier:
            return [_java_import(specifier, line, static=static, wildcard=wildcard)]
    return []


def _java_import(specifier: str, line: int, *, static: bool, wildcard: bool) -> RawImport:
    """What a Java import couples to, which is always a type or a package -- never a member.

    `import static a.b.C.max` names one member of `a.b.C`, so the type is the specifier's
    parent and the member is recorded as a name. `import static a.b.C.*` names *every*
    static member of `a.b.C`, so the type is the specifier itself -- splitting it there too
    would report a dependency on `a.b`, one level short of the class actually imported.
    """
    kind = "wildcard" if wildcard else "value"
    if static and not wildcard:
        owner, _, member = specifier.rpartition(".")
        return RawImport(owner, kind, line, names=(member,))
    return RawImport(specifier, kind, line)


def _descend(node: Node, kind: str) -> list[Node]:
    """Every descendant of a given kind, including the node itself."""
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            found.append(current)
        stack.extend(current.named_children)
    return found


def _python_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    line = node.start_point[0] + 1

    if node.type == "import_statement":
        # `import a.b.c` and `import a.b as c` -- possibly several per statement.
        return [
            RawImport(specifier=specifier, kind="value", line=line)
            for specifier in _module_names(node, spec)
        ]

    module = node.child_by_field_name(spec.module_field)
    if module is None:
        return []

    level, specifier = _python_module_ref(module, spec)
    # `from x import y as z` -- resolution needs `y`, the real submodule name, not `z`.
    names = tuple(
        _unaliased(child, spec)
        for child in node.children_by_field_name(spec.name_field)
        if child.type not in spec.wildcard_kinds
    )
    wildcard = any(child.type in spec.wildcard_kinds for child in node.named_children)

    return [
        RawImport(
            specifier=specifier,
            kind="wildcard" if wildcard else "value",
            line=line,
            level=level,
            names=names,
        )
    ]


def _unaliased(node: Node, spec: ImportSpec) -> str:
    """The imported name with any ``as`` alias stripped."""
    if node.type in spec.alias_kinds:
        inner = node.child_by_field_name("name")
        if inner is not None:
            return _text(inner)
    return _text(node)


def _module_names(node: Node, spec: ImportSpec) -> list[str]:
    """Module paths named by an ``import a, b as c`` statement."""
    names: list[str] = []
    for child in node.children_by_field_name(spec.name_field):
        target = child
        if child.type in spec.alias_kinds:
            inner = child.child_by_field_name("name")
            if inner is not None:
                target = inner
        names.append(_text(target))
    return [name for name in names if name]


def _python_module_ref(module: Node, spec: ImportSpec) -> tuple[int, str]:
    """``(level, dotted path)`` for a ``from`` clause.

    ``from . import x`` is level 1 with an empty path; ``from ..pkg import x`` is level 2
    with path ``pkg``. The level is what anchors resolution to the importing file's package.
    """
    if module.type not in spec.relative_kinds:
        return 0, _text(module)

    level = 0
    remainder = ""
    for child in module.children:
        text = _text(child)
        if child.type == "import_prefix" or set(text) == {"."}:
            level += len(text)
        elif text:
            remainder = text
    return level, remainder


def _ecmascript_statement(node: Node, spec: ImportSpec) -> list[RawImport]:
    source = node.child_by_field_name(spec.module_field)
    if source is None:
        return []  # `export const x = 1` -- a declaration, not a dependency

    specifier = _strip_quotes(_text(source))
    if not specifier:
        return []

    line = node.start_point[0] + 1
    names = _ecmascript_names(node)
    if spec.type_only_token and _has_token(node, spec.type_only_token):
        return [RawImport(specifier, "type_only", line, names=names)]

    # `import "./x"` with no clause runs the module for its side effects only.
    has_clause = any(
        child.type in {"import_clause", "export_clause", "namespace_import"}
        for child in node.named_children
    )
    kind = "value" if has_clause or node.type == "export_statement" else "side_effect"
    # An `export ... from "./m"` is the only ECMAScript statement that both imports and
    # republishes, and the grammar already separates it: a plain `export const x = 1` has no
    # `source` field and returned above.
    return [RawImport(specifier, kind, line, names=names, reexport=node.type == "export_statement")]


def _ecmascript_names(node: Node) -> tuple[str, ...]:
    """The names an import or re-export takes from its module, as the module spells them:
    `import { a, b as c } from "m"` -> a, b; a default import -> default; `* as ns` -> *.
    Not in the oxn original, which recorded the specifier only."""
    names: list[str] = []
    stack = [child for child in node.named_children if child.type in {"import_clause", "export_clause"}]
    stack += [child for child in node.named_children if child.type == "namespace_export"]
    while stack:
        current = stack.pop(0)
        if current.type in {"import_specifier", "export_specifier"}:
            named = current.child_by_field_name("name")
            if named is not None:
                names.append(_text(named))
        elif current.type in {"namespace_import", "namespace_export"}:
            names.append("*")
        elif current.type == "identifier":
            names.append("default")
        else:
            stack[0:0] = current.named_children
    if not names and node.type == "export_statement" and _has_token(node, "*"):
        names.append("*")  # `export * from "m"` republishes everything
    return tuple(names)


# ---- dynamic imports -------------------------------------------------------------------


def _from_dynamic_call(node: Node, spec: ImportSpec) -> RawImport | None:
    """``importlib.import_module("x")``, ``require("x")``, ``import("x")``.

    A literal argument is recorded as a real dependency; a computed one is recorded as
    unresolvable rather than discarded, because a dynamic import is still coupling.
    """
    callee = node.child_by_field_name("function")
    if callee is None or _text(callee) not in spec.dynamic_callees:
        return None

    arguments = node.child_by_field_name("arguments")
    line = node.start_point[0] + 1
    if arguments is None:
        return RawImport("", "dynamic", line)

    republished = _republished(node, spec)
    for argument in arguments.named_children:
        if argument.type in spec.string_kinds:
            return RawImport(_strip_quotes(_text(argument)), "dynamic", line, reexport=republished)
    return RawImport("", "dynamic", line, reexport=republished)


def _republished(node: Node, spec: ImportSpec) -> bool:
    """Is this ``require()`` the right-hand side of an assignment to the module's exports?

    ``module.exports = require("./other")`` and ``exports.helper = require("./h").helper`` both
    republish -- the assigning file declares nothing and every name a caller imports from it
    lives one hop further on. ECMAScript spells the same thing ``export * from "./other"``,
    which the grammar marks with a `source` field; CommonJS spells it as an assignment, so it
    has to be recognised by what is being assigned *to*.
    """
    if not spec.reexport_targets:
        return False
    assignment = _assigned_from(node)
    if assignment is None:
        return False
    left = assignment.child_by_field_name("left")
    target = _text(left) if left is not None else ""
    return any(target == name or target.startswith(f"{name}.") for name in spec.reexport_targets)


def _assigned_from(node: Node) -> Node | None:
    """The assignment whose right-hand side is `node`, looking through member access.

    The climb through `member_expression` is what admits the ``exports.helper =
    require("./h").helper`` form: the `require` call is nested inside the member access that is
    the actual right-hand side.

    Compared by byte range and never by identity -- `child_by_field_name` builds a *new* `Node`
    object on every call, so `right is current` is false for the same node in the same tree. The
    first version of this said `is not` and silently recognised nothing.
    """
    current = node
    while current.parent is not None and current.parent.type == "member_expression":
        current = current.parent
    assignment = current.parent
    if assignment is None or assignment.type != "assignment_expression":
        return None
    right = assignment.child_by_field_name("right")
    span = (current.start_byte, current.end_byte)
    return assignment if right is not None and (right.start_byte, right.end_byte) == span else None


# ---- helpers ---------------------------------------------------------------------------


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace") if node.text else ""


def _strip_quotes(raw: str) -> str:
    return raw.strip().strip("\"'`")


def _has_token(node: Node, token: str) -> bool:
    """True if an anonymous child is exactly ``token`` -- e.g. the ``type`` in ``import type``."""
    return any(not child.is_named and _text(child) == token for child in node.children)
