"""What a file's imports let it write, and what its types are built on.

`imports.py` records that a file depends on a module. The graph needs more: the *name*
the file may now write, the module it came from, and what it is called there
(`from .models import Response as R` lets the file write `R`, which is `Response` in
`models`). It also needs to know what a file republishes, since a name imported from a
package's `__init__`, a barrel `index.ts` or a `pub use` is declared somewhere else.

Read with a walk of the whole tree: imports sit inside functions and conditions too.
How Go, Rust and Java spell an import follows oxn (`resolve/importers.py`, v0.1.2, MIT,
same author).
"""
from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from tree_sitter import Node, Tree

from . import query
from .profile import LanguageProfile


@dataclass(frozen=True)
class Binding:
    local: str            # the name this file may write; '*' for every name of the module at once
    source: str           # the module, as written; '' for the file itself (an export) or the crate's own root
    name: str             # what it is called in the module; '' when `local` stands for the module itself
    level: int = 0        # Python: the number of leading dots. Rust: how many inline `mod` blocks it is written inside
    kind: str = "import"  # 'import'; 'reexport': taken from a module only to be published (`export { x } from`,
    #                       `export * from`); 'export': the file publishes its own `name` as `local`;
    #                       'module': Rust's `mod x;`; 'package': the package the file says it belongs to (in `local`)


_ECMA = frozenset({"typescript", "tsx", "javascript"})
_GO_MAJOR = re.compile(r"^v[0-9]+$")
_GO_DOTTED_MAJOR = re.compile(r"^(?P<name>.+)\.v[0-9]+$")


def facts(profile: LanguageProfile, tree: Tree, data: bytes) -> tuple[list[Binding], list[tuple[str, str]], list[tuple[str, str, str]]]:
    """`bindings`, `bases` and `fields` of one file, from one pass over its tree."""
    family = "ecma" if profile.name in _ECMA else profile.name
    found = query.by_role(profile.name, tree, {
        "binds": _STATEMENTS.get(family, ()), "types": _TYPE_NODES.get(family, ()), "owners": _OWNERS.get(family, ()),
        "assigns": _ASSIGNMENTS.get(family, ()), "generic": TYPE_PARAMETERS})
    generic = type_parameter_names(found.get("generic", []))
    in_order = lambda nodes: sorted(nodes, key=lambda node: (node.start_byte, -node.end_byte))  # noqa: E731
    assigned = in_order(found.get("assigns", []))
    return (_bindings(family, in_order(found.get("binds", []))), _bases(family, in_order(found.get("types", []))),
            _fields(family, in_order(found.get("owners", [])), assigned, [node.start_byte for node in assigned], generic))


def bindings(profile: LanguageProfile, tree: Tree, data: bytes) -> list[Binding]:
    """Every name the file's imports bind, in source order, and what the file republishes under another name."""
    return facts(profile, tree, data)[0]


def _bindings(family: str, statements: list[Node]) -> list[Binding]:
    read = _READERS.get(family)
    out: list[Binding] = []
    for node in statements if read is not None else ():     # wherever they stand: imports hide in functions too
        found = list(read(node))
        if found and family == "rust":
            depth, above = 0, node.parent
            while above is not None:
                depth += above.type == "mod_item"
                above = above.parent
            found = [Binding(b.local, b.source, b.name, depth, b.kind) for b in found]
        out.extend(found)
    return out


# The assignments that can give a field its type, where a language has no field declarations to say it.
_ASSIGNMENTS: dict[str, tuple[str, ...]] = {"python": ("assignment",), "ecma": ("assignment_expression",)}
# The statements that can bind a name from elsewhere, per language.
_STATEMENTS: dict[str, tuple[str, ...]] = {
    "python": ("import_statement", "import_from_statement"),
    "ecma": ("import_statement", "export_statement", "variable_declarator", "assignment_expression"),
    "go": ("package_clause", "import_spec"),
    "rust": ("use_declaration", "extern_crate_declaration", "mod_item"),
    "java": ("package_declaration", "import_declaration"),
    "c": ("preproc_include",), "cpp": ("preproc_include",),
}


# ---- Python --------------------------------------------------------------------------------

def _python(node: Node) -> Iterator[Binding]:
    if node.type == "import_statement":
        for child in node.named_children:
            if child.type == "dotted_name":
                first = _text(child).split(".")[0]
                yield Binding(first, first, "")
            elif child.type == "aliased_import":
                name, alias = child.child_by_field_name("name"), child.child_by_field_name("alias")
                if name is not None and alias is not None:
                    yield Binding(_text(alias), _text(name), "")
    elif node.type == "import_from_statement":
        module = node.child_by_field_name("module_name")
        level, source = 0, ""
        if module is not None and module.type == "relative_import":
            for child in module.children:
                if child.type == "import_prefix":
                    level = _text(child).count(".")
                elif child.type == "dotted_name":
                    source = _text(child)
        elif module is not None:
            source = _text(module)
        if any(child.type == "wildcard_import" for child in node.children):
            yield Binding("*", source, "*", level)
        for child in node.children_by_field_name("name"):
            if child.type == "aliased_import":
                name, alias = child.child_by_field_name("name"), child.child_by_field_name("alias")
                if name is not None and alias is not None:
                    yield Binding(_text(alias), source, _text(name), level)
            else:
                yield Binding(_text(child), source, _text(child), level)


# ---- TypeScript and JavaScript ---------------------------------------------------------------

def _ecma(node: Node) -> Iterator[Binding]:
    kind = node.type
    if kind == "import_statement":
        source = _string(node.child_by_field_name("source"))
        for child in node.named_children:
            if child.type == "import_clause" and source:
                yield from _import_clause(child, source)
            elif child.type == "import_require_clause":      # import x = require('./m')
                name = next((part for part in child.named_children if part.type == "identifier"), None)
                inner = _string(child.child_by_field_name("source"))
                if name is not None and inner:
                    yield Binding(_text(name), inner, "")
    elif kind == "export_statement":
        yield from _export(node)
    elif kind == "variable_declarator":
        yield from _required(node)
    elif kind == "assignment_expression":
        yield from _commonjs_export(node)


def _import_clause(clause: Node, source: str) -> Iterator[Binding]:
    for part in clause.named_children:
        if part.type == "identifier":
            yield Binding(_text(part), source, "default")
        elif part.type == "namespace_import":
            name = next((inner for inner in part.named_children if inner.type == "identifier"), None)
            if name is not None:
                yield Binding(_text(name), source, "")
        elif part.type == "named_imports":
            for specifier in part.named_children:
                if specifier.type != "import_specifier":
                    continue
                name, alias = specifier.child_by_field_name("name"), specifier.child_by_field_name("alias")
                if name is not None:
                    yield Binding(_text(alias or name), source, _text(name))


def _export(node: Node) -> Iterator[Binding]:
    source = _string(node.child_by_field_name("source"))
    clause = next((child for child in node.named_children if child.type == "export_clause"), None)
    namespace = next((child for child in node.named_children if child.type == "namespace_export"), None)
    if source:
        if clause is not None:
            for specifier in clause.named_children:
                name, alias = specifier.child_by_field_name("name"), specifier.child_by_field_name("alias")
                if name is not None:
                    yield Binding(_text(alias or name), source, _text(name), kind="reexport")
        elif namespace is not None:
            name = next((inner for inner in namespace.named_children if "identifier" in inner.type), None)
            if name is not None:
                yield Binding(_text(name), source, "", kind="reexport")
        elif any(child.type == "*" for child in node.children):
            yield Binding("*", source, "*", kind="reexport")
        return
    if clause is not None:
        for specifier in clause.named_children:
            name, alias = specifier.child_by_field_name("name"), specifier.child_by_field_name("alias")
            if name is not None and alias is not None and _text(alias) != _text(name):
                yield Binding(_text(alias), "", _text(name), kind="export")
        return
    is_default = any(child.type == "default" for child in node.children)
    equals = any(child.type == "=" for child in node.children)           # TypeScript's `export = name`
    if not is_default and not equals:
        return
    value = node.child_by_field_name("value") or node.child_by_field_name("declaration")
    if value is None:
        value = next((child for child in node.named_children if child.type == "identifier"), None)
    named = _own_name(value)
    if named:
        yield Binding("default", "", named, kind="export")


def _own_name(value: Node | None) -> str:
    """The name of what is exported whole: an identifier, or a function or class that has one."""
    if value is None:
        return ""
    if value.type == "identifier":
        return _text(value)
    if "function" in value.type or "class" in value.type:
        name = value.child_by_field_name("name")
        return _text(name) if name is not None else ""
    return ""


def _require_source(value: Node | None) -> str:
    if value is None or value.type != "call_expression":
        return ""
    function, arguments = value.child_by_field_name("function"), value.child_by_field_name("arguments")
    if function is None or _text(function) != "require" or arguments is None or arguments.named_child_count != 1:
        return ""
    return _string(arguments.named_children[0])


def _required(node: Node) -> Iterator[Binding]:
    """`const x = require('./m')`, `const { a, b: c } = require('./m')`, `const one = require('./m').one`."""
    name, value = node.child_by_field_name("name"), node.child_by_field_name("value")
    if name is None or value is None:
        return
    member = ""
    if value.type == "member_expression":
        inner, wanted = value.child_by_field_name("object"), value.child_by_field_name("property")
        if inner is None or wanted is None:
            return
        value, member = inner, _text(wanted)
    source = _require_source(value)
    if not source:
        return
    if name.type == "identifier":
        yield Binding(_text(name), source, member)
    elif name.type == "object_pattern" and not member:
        for part in name.named_children:
            if part.type == "shorthand_property_identifier_pattern":
                yield Binding(_text(part), source, _text(part))
            elif part.type == "pair_pattern":
                key, local = part.child_by_field_name("key"), part.child_by_field_name("value")
                if key is not None and local is not None and local.type == "identifier":
                    yield Binding(_text(local), source, _text(key))


def _commonjs_export(node: Node) -> Iterator[Binding]:
    """`module.exports = …` and `exports.name = …`, where they give something another name."""
    left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
    if left is None or right is None or left.type != "member_expression":
        return
    target = _text(left).replace(" ", "")
    if target == "module.exports":
        source = _require_source(right)
        if source:
            yield Binding("*", source, "*", kind="reexport")
        elif right.type == "object":
            for pair in right.named_children:
                if pair.type != "pair":
                    continue
                key, value = pair.child_by_field_name("key"), pair.child_by_field_name("value")
                if key is not None and value is not None and value.type == "identifier" and _text(value) != _text(key):
                    yield Binding(_text(key), "", _text(value), kind="export")
        else:
            named = _own_name(right)
            if named:
                yield Binding("default", "", named, kind="export")
    elif target.startswith(("exports.", "module.exports.")):
        published = target.rsplit(".", 1)[1]
        source = _require_source(right)
        if source:
            yield Binding(published, source, "")
        elif right.type == "identifier" and _text(right) != published:
            yield Binding(published, "", _text(right), kind="export")


# ---- Go --------------------------------------------------------------------------------------

def _go_package_name(path: str) -> str:
    """The name an unaliased Go import binds, by Go's convention: the last segment of the path,
    a `/v2` or `.v2` version apart. The real name is the target's `package` clause; the graph
    corrects this where the package is in the tree."""
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) > 1 and _GO_MAJOR.match(segments[-1]):
        segments.pop()
    tail = segments[-1] if segments else ""
    dotted = _GO_DOTTED_MAJOR.match(tail)
    return dotted.group("name") if dotted else tail


def _go(node: Node) -> Iterator[Binding]:
    if node.type == "package_clause":
        name = next((child for child in node.named_children if child.type == "package_identifier"), None)
        if name is not None:
            yield Binding(_text(name), "", "", kind="package")
    elif node.type == "import_spec":
        path = _string(node.child_by_field_name("path"))
        alias = node.child_by_field_name("name")
        if not path:
            return
        if alias is None:
            yield Binding(_go_package_name(path), path, "")
        elif alias.type == "dot":
            yield Binding("*", path, "*")
        elif alias.type == "package_identifier":
            yield Binding(_text(alias), path, "")


# ---- Rust ------------------------------------------------------------------------------------

def _rust(node: Node) -> Iterator[Binding]:
    if node.type == "use_declaration":
        yield from _rust_use(node.child_by_field_name("argument"), "")
    elif node.type == "extern_crate_declaration":
        name, alias = node.child_by_field_name("name"), node.child_by_field_name("alias")
        if name is not None:
            yield Binding(_text(alias or name), "", _text(name))
    elif node.type == "mod_item" and node.child_by_field_name("body") is None:
        name = node.child_by_field_name("name")
        if name is not None:
            yield Binding(_text(name), "self", _text(name), kind="module")


def _rust_use(node: Node | None, prefix: str) -> Iterator[Binding]:
    """Walk a `use` tree, carrying the path so far: `use x::y::{p, q as r, self}` binds p, r and y."""
    if node is None:
        return
    kind = node.type
    if kind in ("identifier", "self", "crate", "super"):
        yield from _rust_leaf(_text(node), _text(node), prefix)
    elif kind == "scoped_identifier":
        name = node.child_by_field_name("name")
        inner = _join(prefix, _text(node.child_by_field_name("path")) if node.child_by_field_name("path") else "")
        if name is not None:
            yield from _rust_leaf(_text(name), _text(name), inner)
    elif kind == "use_as_clause":
        alias, target = node.child_by_field_name("alias"), node.child_by_field_name("path")
        if alias is None or target is None:
            return
        if target.type == "scoped_identifier":
            name, path = target.child_by_field_name("name"), target.child_by_field_name("path")
            if name is not None:
                yield Binding(_text(alias), _join(prefix, _text(path) if path is not None else ""), _text(name))
        else:
            yield Binding(_text(alias), prefix, _text(target))
    elif kind == "use_wildcard":
        inner = next((child for child in node.named_children), None)
        yield Binding("*", _join(prefix, _text(inner) if inner is not None else ""), "*")
    elif kind in ("use_list", "scoped_use_list"):
        listed = node.child_by_field_name("list") or node
        path = node.child_by_field_name("path")
        inner = _join(prefix, _text(path) if path is not None else "")
        for child in listed.named_children:
            yield from _rust_use(child, inner)


def _rust_leaf(local: str, name: str, source: str) -> Iterator[Binding]:
    if name == "self":                      # `use a::b::{self}` binds `b`, which lives in `a`
        if source:
            head, _, last = source.rpartition("::")
            yield Binding(last, head, last)
    elif name:
        yield Binding(local, source, name)


def _join(prefix: str, path: str) -> str:
    return f"{prefix}::{path}" if prefix and path else path or prefix


# ---- Java ------------------------------------------------------------------------------------

def _java(node: Node) -> Iterator[Binding]:
    if node.type == "package_declaration":
        name = next((child for child in node.named_children if "identifier" in child.type), None)
        if name is not None:
            yield Binding(_text(name), "", "", kind="package")
    elif node.type == "import_declaration":
        path = next((child for child in node.named_children if "identifier" in child.type), None)
        if path is None:
            return
        if any(child.type == "asterisk" for child in node.named_children):
            yield Binding("*", _text(path), "*")
        elif path.type == "scoped_identifier":
            name, scope = path.child_by_field_name("name"), path.child_by_field_name("scope")
            if name is not None and scope is not None:
                yield Binding(_text(name), _text(scope), _text(name))


# ---- C and C++ -------------------------------------------------------------------------------

def _c(node: Node) -> Iterator[Binding]:
    if node.type == "preproc_include":
        path = node.child_by_field_name("path")
        if path is not None and path.type == "string_literal":     # "local.h", not <system>
            yield Binding("*", _text(path).strip('"'), "*")


_READERS = {"python": _python, "ecma": _ecma, "go": _go, "rust": _rust, "java": _java, "c": _c, "cpp": _c}


# ---- what a type is built on -----------------------------------------------------------------

_TYPE_NODES: dict[str, frozenset[str]] = {
    "python": frozenset({"class_definition"}),
    "ecma": frozenset({"class_declaration", "class", "abstract_class_declaration", "interface_declaration"}),
    "java": frozenset({"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}),
    "go": frozenset({"type_spec"}),
    "rust": frozenset({"impl_item", "trait_item"}),
    "cpp": frozenset({"class_specifier", "struct_specifier"}),
}
_NESTS = frozenset({"class_definition", "class_declaration", "abstract_class_declaration", "interface_declaration", "class",
                    "enum_declaration", "record_declaration", "class_specifier", "struct_specifier", "mod_item", "namespace_definition"})


def bases(profile: LanguageProfile, tree: Tree, data: bytes) -> list[tuple[str, str]]:
    """(type, what it extends, implements or embeds), both as written, the type qualified within the file."""
    return facts(profile, tree, data)[1]


def _bases(family: str, types: list[Node]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for node in types:
        name, built_on = _type_and_bases(family, node)
        if name:
            prefix = _prefix(node)
            out.extend((prefix + name, base) for base in built_on if base)
    return out


def _prefix(node: Node) -> str:
    """`Outer.` for a node inside the type or module `Outer`: how the file qualifies what is declared there."""
    names: list[str] = []
    above = node.parent
    while above is not None:
        if above.type in _NESTS:
            own = above.child_by_field_name("name")
            if own is not None:
                names.append(_text(own))
        above = above.parent
    return "".join(name + "." for name in reversed(names))


def _type_and_bases(family: str, node: Node) -> tuple[str, list[str]]:
    name = node.child_by_field_name("name")
    own = _text(name) if name is not None else ""
    found: list[str] = []
    if family == "python":
        arguments = node.child_by_field_name("superclasses")
        found = [_text(child) for child in (arguments.named_children if arguments is not None else [])
                 if child.type in ("identifier", "attribute")]
    elif family == "ecma":
        for child in node.named_children:
            if child.type in ("class_heritage", "extends_type_clause"):
                found.extend(_heritage(child))
    elif family == "java":
        for field in ("superclass", "interfaces"):
            holder = node.child_by_field_name(field)
            if holder is not None:
                found.extend(_type_names(holder))
        for child in node.named_children:
            if child.type == "extends_interfaces":
                found.extend(_type_names(child))
    elif family == "go":
        body = node.child_by_field_name("type")
        if body is not None and body.type == "struct_type":
            for declared in _descend(body, "field_declaration"):
                if declared.child_by_field_name("name") is None:        # an embedded type: its methods become this one's
                    kind = declared.child_by_field_name("type")
                    found.append(_bare(_text(kind)) if kind is not None else "")
        elif body is not None and body.type == "interface_type":
            for element in body.named_children:
                if element.type in ("type_elem", "constraint_elem", "type_identifier", "qualified_type"):
                    found.append(_bare(_text(element)))
    elif family == "rust":
        if node.type == "impl_item":
            trait, kind = node.child_by_field_name("trait"), node.child_by_field_name("type")
            if trait is None or kind is None:
                return "", []
            return _bare(_text(kind)), [_bare(_text(trait))]
        bounds = node.child_by_field_name("bounds")
        found = [_bare(_text(child)) for child in (bounds.named_children if bounds is not None else [])
                 if child.type in ("type_identifier", "scoped_type_identifier", "generic_type")]
    elif family == "cpp":
        for child in node.named_children:
            if child.type == "base_class_clause":
                found.extend(_bare(_text(part)) for part in child.named_children
                             if part.type in ("type_identifier", "qualified_identifier", "template_type"))
    return own, found


def _heritage(node: Node) -> Iterator[str]:
    """`extends B<T> implements I, J` (TypeScript) or `extends ns.B` (JavaScript)."""
    clauses = [child for child in node.named_children if child.type in ("extends_clause", "implements_clause")]
    if not clauses:                                   # JavaScript: the heritage holds the expression itself
        yield from (_bare(_text(child)) for child in node.named_children
                    if child.type in ("identifier", "member_expression", "type_identifier", "generic_type", "nested_type_identifier"))
        return
    for clause in clauses:
        for child in clause.named_children:
            if child.type != "type_arguments":
                yield _bare(_text(child))


def _type_names(holder: Node) -> Iterator[str]:
    stack = [holder]
    while stack:
        node = stack.pop()
        if node.type in ("type_identifier", "scoped_type_identifier", "generic_type"):
            yield _bare(_text(node))
        else:
            stack.extend(reversed(node.named_children))


# ---- the types a type declares for its fields --------------------------------------------------

_OWNERS: dict[str, frozenset[str]] = {
    "python": frozenset({"class_definition"}),
    "ecma": frozenset({"class_declaration", "class", "abstract_class_declaration"}),
    "java": frozenset({"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}),
    "go": frozenset({"type_spec"}),
    "rust": frozenset({"struct_item"}),
    "cpp": frozenset({"class_specifier", "struct_specifier"}),
}
_NAME = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::)[A-Za-z_$][\w$]*)*$")
_SEES_THROUGH = ("Box", "Rc", "Arc", "Optional")      # a call on one of these reaches what it holds
_NOT_THERE = frozenset({"None", "null", "undefined"})


def named_type(text: str) -> str:
    """A type as written, as the one name a call on a value of it would be looked up under:
    `*generic.Gauge` -> `generic.Gauge`, `Optional[Store]` -> `Store`, `&'a Matcher` -> `Matcher`,
    `Map<String, Pet>` -> `Map`. '' when it is not one named type (`[]Item`, `() => void`, `A | B`)."""
    kept = [part for part in _split_union(" ".join(text.split()).strip("\"'")) if part not in _NOT_THERE]
    if len(kept) != 1:
        return ""
    name = kept[0]
    for _ in range(4):
        name = re.sub(r"^(?:[*&]+\s*|'\w+\s+|(?:mut|dyn|impl|final|const|readonly)\s+)+", "", name)
        inside = re.match(r"^(\w+)\s*[<\[](.*)[>\]]$", name)
        if inside is None or inside.group(1) not in _SEES_THROUGH:
            break
        name = inside.group(2).strip()
    base = re.split(r"[<\[]", name, maxsplit=1)[0].strip()
    return base if _NAME.match(base) else ""


def constructed(callee: str) -> str:
    """How a value built by a call is noted: `Engine()`. The graph decides whether `Engine` is a type."""
    name = "".join(callee.split())
    return name + "()" if _NAME.match(name) else ""


def _split_union(text: str) -> list[str]:
    parts, depth, start = [], 0, 0
    for index, char in enumerate(text):
        if char in "<[(":
            depth += 1
        elif char in ">])":
            depth -= 1
        elif char == "|" and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    return parts + [text[start:].strip()]


def fields(profile: LanguageProfile, tree: Tree, data: bytes) -> list[tuple[str, str, str]]:
    """(type, field, the field's type as `named_type` gives it), the type qualified within the file. Only
    fields whose type the source states: a declaration, a constructor parameter kept as a field, or an
    assignment from a constructor call or a typed parameter."""
    return facts(profile, tree, data)[2]


# Where a function, a method or a type declares names that stand for types.
TYPE_PARAMETERS = ("type_parameter", "type_parameter_declaration", "constrained_type_parameter", "optional_type_parameter")


def type_parameter_names(nodes: list[Node]) -> set[str]:
    """`T` of `class Repo<T>` or `func F[T any]`: a name for a type, never one of the file's types. (Python's
    grammar calls the arguments of `Optional[Store]` by the same name: those are uses, not declarations.)"""
    return {_text(node.child_by_field_name("name") or (node.named_children[0] if node.named_children else node))
            for node in nodes if node.parent is None or node.parent.type != "generic_type"}


def _fields(family: str, owners: list[Node], assigned: list[Node], starts: list[int],
            generic: set[str] = frozenset()) -> list[tuple[str, str, str]]:
    said: dict[tuple[str, str], list[tuple[str, bool]]] = {}      # (type, field) -> (a type, whether it is declared)

    def inside(node: Node | None) -> list[Node]:
        """The assignments under `node`, in source order."""
        if node is None:
            return []
        return assigned[bisect.bisect_left(starts, node.start_byte):bisect.bisect_left(starts, node.end_byte)]

    for node in owners:
        own = node.child_by_field_name("name")
        if own is None:
            continue
        owner = _prefix(node) + _text(own)
        for name, kind, declared in _FIELD_READERS[family](node, inside):
            if name:
                said.setdefault((owner, name), []).append((kind, declared))
    out: list[tuple[str, str, str]] = []
    for (owner, name), stated in said.items():
        declared = [kind for kind, is_declared in stated if is_declared]
        inferred = {kind for kind, _ in stated}
        kind = declared[0] if declared else next(iter(inferred)) if len(inferred) == 1 else ""   # given two things: no one type
        if kind and kind not in generic:
            out.append((owner, name, kind))
    return out


def _go_fields(node: Node, inside: Callable[[Node | None], list[Node]]) -> Iterator[tuple[str, str, bool]]:
    body = node.child_by_field_name("type")
    if body is None or body.type != "struct_type":
        return
    for declared in _descend(body, "field_declaration"):
        kind = declared.child_by_field_name("type")
        for name in declared.children_by_field_name("name"):
            yield _text(name), named_type(_text(kind)), True


def _rust_fields(node: Node, inside: Callable[[Node | None], list[Node]]) -> Iterator[tuple[str, str, bool]]:
    body = node.child_by_field_name("body")
    for declared in (_descend(body, "field_declaration") if body is not None else ()):
        yield _text(declared.child_by_field_name("name")), named_type(_text(declared.child_by_field_name("type"))), True


def _java_fields(node: Node, inside: Callable[[Node | None], list[Node]]) -> Iterator[tuple[str, str, bool]]:
    body = node.child_by_field_name("body")
    for declared in (body.named_children if body is not None else ()):
        if declared.type != "field_declaration":
            continue
        kind = named_type(_text(declared.child_by_field_name("type")))
        for declarator in declared.children_by_field_name("declarator"):
            yield _text(declarator.child_by_field_name("name")), kind, True


def _cpp_fields(node: Node, inside: Callable[[Node | None], list[Node]]) -> Iterator[tuple[str, str, bool]]:
    body = node.child_by_field_name("body")
    for declared in (body.named_children if body is not None else ()):
        if declared.type != "field_declaration":
            continue
        kind = named_type(_text(declared.child_by_field_name("type")))
        for declarator in declared.children_by_field_name("declarator"):
            name = declarator
            while name is not None and name.type != "field_identifier":
                name = name.child_by_field_name("declarator")
            if name is not None:
                yield _text(name), kind, True


def _annotation(node: Node | None) -> str:
    """The type inside a TypeScript `: Type` annotation."""
    if node is None:
        return ""
    return named_type(_text(node.named_children[0])) if node.type == "type_annotation" and node.named_children else named_type(_text(node))


def _ecma_fields(node: Node, inside: Callable[[Node | None], list[Node]]) -> Iterator[tuple[str, str, bool]]:
    body = node.child_by_field_name("body")
    for member in (body.named_children if body is not None else ()):
        if member.type in ("public_field_definition", "field_definition"):
            name = member.child_by_field_name("name") or member.child_by_field_name("property")
            value = member.child_by_field_name("value")
            annotated = member.child_by_field_name("type") is not None
            kind = _annotation(member.child_by_field_name("type"))
            if not annotated and value is not None and value.type == "new_expression":
                kind = named_type(_text(value.child_by_field_name("constructor")))
            yield _text(name), kind, annotated
        elif member.type == "method_definition":
            typed: dict[str, str] = {}
            parameters = member.child_by_field_name("parameters")
            for parameter in (parameters.named_children if parameters is not None else ()):
                pattern = parameter.child_by_field_name("pattern")
                if pattern is None or pattern.type != "identifier":
                    continue
                kind = _annotation(parameter.child_by_field_name("type"))
                typed[_text(pattern)] = kind
                if any(child.type in ("accessibility_modifier", "readonly", "override_modifier") for child in parameter.children):
                    yield _text(pattern), kind, True    # constructor(private readonly svc: Service)
            for assigned in inside(member.child_by_field_name("body")):
                left, right = assigned.child_by_field_name("left"), assigned.child_by_field_name("right")
                if left is None or right is None or left.type != "member_expression":
                    continue
                holder, name = left.child_by_field_name("object"), left.child_by_field_name("property")
                if holder is None or holder.type != "this" or name is None:
                    continue
                if right.type == "new_expression":
                    yield _text(name), named_type(_text(right.child_by_field_name("constructor"))), False
                else:
                    yield _text(name), typed.get(_text(right), "") if right.type == "identifier" else "", False


def _python_fields(node: Node, inside: Callable[[Node | None], list[Node]]) -> Iterator[tuple[str, str, bool]]:
    body = node.child_by_field_name("body")
    for member in (body.named_children if body is not None else ()):
        if member.type == "expression_statement" and member.named_children:
            member = member.named_children[0]
        if member.type == "assignment":
            assigned = member
            left, kind = assigned.child_by_field_name("left"), assigned.child_by_field_name("type")
            if left is not None and left.type == "identifier" and kind is not None:
                yield _text(left), named_type(_text(kind)), True
            continue
        method = member.child_by_field_name("definition") if member.type == "decorated_definition" else member
        if method is None or method.type != "function_definition":
            continue
        parameters = method.child_by_field_name("parameters")
        listed = parameters.named_children if parameters is not None else []
        if not listed or listed[0].type != "identifier":
            continue
        receiver = _text(listed[0])
        typed = {}
        passed = set()      # every parameter's name: a factory passed in is not the file's function of that name
        for parameter in listed[1:]:
            name = parameter if parameter.type == "identifier" else \
                parameter.child_by_field_name("name") or (parameter.named_children[0] if parameter.named_children else None)
            passed.add(_text(name))
            if parameter.type in ("typed_parameter", "typed_default_parameter"):
                typed[_text(name)] = named_type(_text(parameter.child_by_field_name("type")))
        for assigned in inside(method.child_by_field_name("body")):
            left, right, kind = (assigned.child_by_field_name(field) for field in ("left", "right", "type"))
            if left is None or left.type != "attribute" or _text(left.child_by_field_name("object")) != receiver:
                continue
            name = _text(left.child_by_field_name("attribute"))
            if kind is not None:
                yield name, named_type(_text(kind)), True
            elif right is not None and right.type == "call":
                built = constructed(_text(right.child_by_field_name("function")))
                yield name, "" if re.split(r"\.", built)[0].removesuffix("()") in passed else built, False
            elif right is not None:
                yield name, typed.get(_text(right), "") if right.type == "identifier" else "", False


_FIELD_READERS = {"python": _python_fields, "ecma": _ecma_fields, "java": _java_fields, "go": _go_fields, "rust": _rust_fields,
                  "cpp": _cpp_fields}


def _descend(node: Node, kind: str) -> Iterator[Node]:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == kind:
            yield current
        else:
            stack.extend(reversed(current.named_children))


def _bare(text: str) -> str:
    """A type as a name: no type arguments, no pointer or reference marks."""
    return text.split("<", 1)[0].split("[", 1)[0].lstrip("*&").strip()


def _string(node: Node | None) -> str:
    return _text(node).strip().strip("\"'`") if node is not None else ""


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None and node.text else ""
