"""Call sites: where a file calls something, read from the same parse as its definitions.

A call is recorded by what is written, nothing more: the callee's own name (`save`), what
it is called on if anything (`self.store`), where the name stands, and the definition the
call sits in. Whether `save` is this file's `save`, an imported one, or one of forty
methods of that name is a question for the graph, which answers it per view and says how
sure it is (ADR-003). Calls with no name of their own (`get()(1)`, `(lambda: 1)()`) and
macro invocations are left out.

Two more things are read here because only the file's own syntax can say them: whether the
call is written through a path (`config::args()`) or on a value (`item.save()`), and what
the first name of the call is bound to inside the function it sits in: the method's own
receiver, a parameter or local variable, or nothing local. A parameter called `filter`
is not the project's `filter` function, and the graph must not say it is.

Which nodes are calls, and which field holds the callee, follows oxn's language profiles
(v0.1.2, MIT, same author).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Node, Tree

from . import query
from .bindings import TYPE_PARAMETERS, constructed, named_type, type_parameter_names
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
FAMILIES = frozenset(_SITES)     # the languages whose calls are read
_NAME_FIELDS = ("attribute", "property", "field", "name")
_OWNER_FIELDS = ("object", "operand", "value", "argument", "path", "scope")
_WRAPPERS = {"generic_function": "function", "template_function": "name", "generic_type": None,
             "scoped_type_identifier": "name", "template_type": "name"}
_CALLABLE = frozenset({"function", "method"})


RECEIVER_LIMIT = 120
_PATHS = frozenset({"scoped_identifier", "qualified_identifier"})
_JS_SCOPES = frozenset({"function_declaration", "function_expression", "arrow_function", "method_definition",
                        "generator_function_declaration", "generator_function"})
# The nodes that open a scope for local names.
_SCOPES: dict[str, frozenset[str]] = {
    "python": frozenset({"function_definition", "lambda"}),
    "typescript": _JS_SCOPES, "tsx": _JS_SCOPES, "javascript": _JS_SCOPES,
    "go": frozenset({"function_declaration", "method_declaration", "func_literal"}),
    "rust": frozenset({"function_item", "closure_expression"}),
    "java": frozenset({"method_declaration", "constructor_declaration", "compact_constructor_declaration", "lambda_expression"}),
    "cpp": frozenset({"function_definition", "lambda_expression"}),
    "c": frozenset({"function_definition"}),
}
# Declarations whose own name belongs to the scope around them.
_NAMED_OUTSIDE = frozenset({"function_definition", "class_definition", "function_declaration", "generator_function_declaration",
                            "class_declaration", "function_item"})
_JS_BINDERS = {"formal_parameters": ("",), "arrow_function": ("parameter",), "variable_declarator": ("name",),
               "catch_clause": ("parameter",), "for_in_statement": ("left",)}
_C_BINDERS = {"parameter_declaration": ("declarator",), "optional_parameter_declaration": ("declarator",),
              "declaration": ("declarator",), "for_range_loop": ("declarator",)}
# Per language: the nodes that bind local names, and the fields holding the names ('' for the node itself).
_BINDERS: dict[str, dict[str, tuple[str, ...]]] = {
    "python": {"parameters": ("",), "lambda_parameters": ("",), "assignment": ("left",), "augmented_assignment": ("left",),
               "for_statement": ("left",), "for_in_clause": ("left",), "named_expression": ("name",), "as_pattern": ("alias",)},
    "typescript": _JS_BINDERS, "tsx": _JS_BINDERS, "javascript": _JS_BINDERS,
    "go": {"parameter_declaration": ("",), "variadic_parameter_declaration": ("",), "var_spec": ("",), "const_spec": ("",),
           "short_var_declaration": ("left",), "range_clause": ("left",), "type_switch_statement": ("alias",),
           "receive_statement": ("left",)},
    "rust": {"parameter": ("pattern",), "closure_parameters": ("",), "let_declaration": ("pattern",), "for_expression": ("pattern",),
             "let_condition": ("pattern",), "match_arm": ("pattern",)},
    "java": {"formal_parameter": ("name",), "spread_parameter": ("",), "local_variable_declaration": ("declarator",),
             "enhanced_for_statement": ("name",), "catch_formal_parameter": ("name",), "lambda_expression": ("parameters",),
             "resource": ("name",)},
    "cpp": _C_BINDERS, "c": _C_BINDERS,
}
# What a pattern is made of: only these are searched for the names it binds.
_PATTERNS = frozenset({
    "pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern", "dictionary_splat_pattern", "typed_parameter",
    "default_parameter", "typed_default_parameter", "parameters", "lambda_parameters", "as_pattern_target",
    "formal_parameters", "required_parameter", "optional_parameter", "assignment_pattern", "rest_pattern", "object_pattern",
    "array_pattern", "pair_pattern", "object_assignment_pattern",
    "expression_list", "parameter_list", "parameter_declaration", "variadic_parameter_declaration", "var_spec", "const_spec",
    "parameter", "closure_parameters", "tuple_struct_pattern", "struct_pattern", "field_pattern", "ref_pattern", "mut_pattern",
    "reference_pattern", "captured_pattern", "slice_pattern", "or_pattern", "match_pattern",
    "formal_parameter", "spread_parameter", "variable_declarator", "inferred_parameters",
    "optional_parameter_declaration", "init_declarator", "pointer_declarator", "reference_declarator", "array_declarator",
})
_PARTS = {"named": 0, "scope": 1, "binds": 2, "typed": 3, "call": 4, "assigns": 5, "imports": 6}
# What holds members: a function declared in one of these inside a function is a method (or a module's
# function), reached through its owner and never by a plain call from the function around.
_TYPE_BODIES = frozenset({"class_definition", "class_declaration", "class", "impl_item", "trait_item", "mod_item"})
_FUNCTION_IMPORTS = {"python": frozenset({"import_statement", "import_from_statement"})}
# A function declared inside a function, and what a local name is bound to when it is bound to a function.
_FUNCTION_DECLARATIONS = frozenset({"function_definition", "function_declaration", "generator_function_declaration", "function_item"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "generator_function", "lambda", "func_literal", "closure_expression"})
# Per language: what gives an existing name another value without binding it anew. In Python an assignment
# binds, but under `nonlocal` or `global` it binds somewhere else than where it is written, and `del` unbinds.
_JS_ASSIGNS = frozenset({"assignment_expression", "augmented_assignment_expression"})
_ASSIGNS: dict[str, frozenset[str]] = {
    "python": frozenset({"nonlocal_statement", "global_statement", "delete_statement"}),
    "typescript": _JS_ASSIGNS, "tsx": _JS_ASSIGNS, "javascript": _JS_ASSIGNS,
    "go": frozenset({"assignment_statement"}), "rust": frozenset({"assignment_expression", "compound_assignment_expr"}),
}
_FILE = 0       # the scope that stands for the file itself
# What binds a name at file level without declaring anything: a loop variable there is not the function it is named like.
_FILE_LEVEL_BINDERS: dict[str, frozenset[str]] = {
    "python": frozenset({"for_statement", "for_in_clause", "as_pattern", "named_expression"}),
    "typescript": frozenset({"for_in_statement", "catch_clause"}), "tsx": frozenset({"for_in_statement", "catch_clause"}),
    "javascript": frozenset({"for_in_statement", "catch_clause"}),
}
_NOT_A_NAME = frozenset({"type", "value", "right", "default_value", "parameters", "size", "key", "dimensions"})
_BOUND_LEAVES = frozenset({"identifier", "shorthand_property_identifier_pattern", "shorthand_field_identifier"})
_OWNER_HEADS = ("object", "operand", "value", "argument", "path", "scope")
_SEE_THROUGH = frozenset({"non_null_expression", "parenthesized_expression"})


@dataclass(frozen=True)
class Call:
    name: str             # the callee's own name, as written
    receiver: str         # what it is called on, as written; '' for a plain call
    line: int             # 1-based, of the name
    column: int           # 0-based byte column of the name: where an index of occurrences looks
    caller: int | None    # index of the innermost function or method it sits in; None at file level
    via: str = ""         # '' a plain call; 'member' called on a value (`a.f()`); 'path' through a path (`a::f()`)
    head: str = ""        # what the first name is bound to where the call stands: 'self' the method's own receiver,
    #                       'super' its base, 'local' a parameter or local name, 'value' no name at all (`f().g()`),
    #                       'inner' a function defined in a function around the call (see `inner`), 'inner-var' the
    #                       same under a name the file assigns to again, '' nothing local: a name of the file, an
    #                       import, a builtin
    receiver_type: str = ""  # for a call on a local name: the type its function declares for it (`generic.Gauge`), or
    #                       the call that built it (`Engine()`); '' when the function does not say
    arity: int = -1       # how many arguments are passed; -1 when the call does not show them
    column16: int = -1    # the column in UTF-16 units and in characters, where text before the name on its line
    column32: int = -1    # makes them differ from the byte column (-1: the same). Other tools count that way
    inner: tuple[tuple[int, int], ...] = ()   # for a call of an inner function: the first and last line of each
    #                       definition the nearest scope binding the name gives it (two: defined on both sides of an `if`)


def calls(profile: LanguageProfile, tree: Tree, data: bytes, found: list[Definition]) -> list[Call]:
    """Every named call in the file, in source order."""
    family = profile.name
    sites = _SITES.get(family)
    if not sites:
        return []
    scopes, binders = _SCOPES.get(family, frozenset()), _BINDERS.get(family, {})
    at_file_level = _FILE_LEVEL_BINDERS.get(family, frozenset())
    # scope -> name -> where it is bound. Scope 0 is the file itself, and holds only what a loop, a
    # comprehension or a handler binds there: the file's own declarations are definitions, not local names.
    bound: dict[int, dict[str, set[int]]] = {_FILE: {}}
    stated: dict[int, dict[str, list[str]]] = {_FILE: {}}   # scope -> name -> each type a statement gives it ('' for none)
    receivers: dict[int, str] = {}      # scope -> the name its method's receiver goes by, where the language names it
    functions: dict[int, dict[str, dict[int, tuple[int, int]]]] = {}   # scope -> name -> where bound -> the function's lines
    assigned: set[str] = set()          # names given another value somewhere in the file
    imported: dict[int, set[str]] = {}  # scope -> names an import inside that function binds
    seen: list[tuple[Node, Node | None, str, int, tuple[int, ...]]] = []
    # Only the nodes that matter are visited, found by the parser's query engine: scopes, what binds or
    # types a local name, and calls. In source order, an outer node first, so a scope is open before what
    # it holds is met; one node can play several parts, taken in the order of _PARTS.
    parts = query.by_role(family, tree, {"named": _NAMED_OUTSIDE, "scope": scopes, "binds": binders,
                                         "typed": _TYPED.get(family, frozenset()), "call": sites,
                                         "assigns": _ASSIGNS.get(family, frozenset()),
                                         "imports": _FUNCTION_IMPORTS.get(family, frozenset()), "generic": TYPE_PARAMETERS})
    generic = type_parameter_names(parts.pop("generic", []))
    events = sorted(((node.start_byte, -node.end_byte, _PARTS[part], node) for part, nodes in parts.items() for node in nodes),
                    key=lambda event: event[:3])
    open_scopes: list[tuple[int, int]] = []    # (where it ends, its id), innermost last
    for start, _, part, node in events:
        while open_scopes and open_scopes[-1][0] <= start:
            open_scopes.pop()
        if part == 0:           # a declaration whose own name belongs to the scope around it
            name = node.child_by_field_name("name")
            if open_scopes and name is not None and not _is_member(node):
                bound[open_scopes[-1][1]].setdefault(_text(name), set()).add(name.start_byte)
                if node.type in _FUNCTION_DECLARATIONS:
                    functions[open_scopes[-1][1]].setdefault(_text(name), {})[name.start_byte] = (node.start_point[0] + 1, node.end_point[0] + 1)
        elif part == 1:         # a scope opens
            open_scopes.append((node.end_byte, node.id))
            bound[node.id], stated[node.id], functions[node.id] = {}, {}, {}
            receiver = _receiver_name(family, node)
            if receiver:
                receivers[node.id] = receiver
                bound[node.id][receiver] = {-1}
        elif part == 2:
            if (open_scopes or node.type in at_file_level) and not _is_require(node):
                into = bound[open_scopes[-1][1] if open_scopes else _FILE]
                for field in binders[node.type]:
                    for holder in ([node] if not field else node.children_by_field_name(field)):
                        for name, where in _bound_names(holder, family):
                            into.setdefault(name, set()).add(where)
                if open_scopes:
                    for name, where, lines in _function_bindings(node):
                        functions[open_scopes[-1][1]].setdefault(name, {})[where] = lines
        elif part == 3:
            if open_scopes:
                known = stated[open_scopes[-1][1]]
                for name, kind in _stated(family, node):
                    known.setdefault(name, []).append(kind)
        elif part == 5:
            left = node.child_by_field_name("left")
            # `f = g` and `a, f = g()`; not `f.cache = {}`, which gives a property of it a value and leaves it what it is.
            written = [left] if left is not None else \
                [each for child in node.named_children for each in (child, *child.named_children)]    # nonlocal a, b; del a
            assigned.update(_text(each) for each in written if each.type == "identifier")
            if left is not None and left.type in _PATTERNS:      # a, f = g(); ({f} = obj); [[f]] = list
                assigned.update(name for name, _ in _bound_names(left, family))
        elif part == 6:
            if open_scopes:
                for named in node.children_by_field_name("name"):
                    alias = named.child_by_field_name("alias")
                    parts = [child for child in (alias or named).named_children if child.type == "identifier"] or [alias or named]
                    imported.setdefault(open_scopes[-1][1], set()).add(_text(parts[0] if node.type == "import_statement" else parts[-1]))
        else:
            callee = node.child_by_field_name(sites[node.type])
            if callee is None:
                continue
            owner = node.child_by_field_name("object") if family == "java" and node.type == "method_invocation" else None
            split = (callee, owner, "member" if owner is not None else "") if owner is not None or _is_name(callee) else _split(callee)
            if split is not None:
                passed = node.child_by_field_name("arguments")
                arity = sum(1 for child in passed.named_children if child.type != "comment") if passed is not None else -1
                # A function sees the file's loop variables only as it would any global; a call at file level is inside them.
                seen.append((*split, arity, tuple(scope for _, scope in open_scopes) or (_FILE,)))
    inside = _innermost(found, [name.start_byte for name, *_ in seen])
    out: list[Call] = []
    for (name, owner, via, arity, chain), caller in zip(seen, inside):
        head, first = _head(name if owner is None else owner, via, chain, bound, receivers)
        kind = _type_of(first, chain, bound, stated, generic) if head == "local" and owner is not None else ""
        lines: tuple[tuple[int, int], ...] = ()
        if head == "local" and owner is None and not via:
            # The nearest scope that binds the name, when every binding of it there is a function: a name also
            # bound as a parameter, or to something else as well, is whatever it is at run time.
            scope = next(scope for scope in reversed(chain) if first in bound[scope])
            made = functions.get(scope, {}).get(first)
            if made and bound[scope][first] <= made.keys() and first not in imported.get(scope, ()):
                head, lines = ("inner-var" if first in assigned else "inner"), tuple(sorted(made.values()))
        before = data[name.start_byte - name.start_point[1]:name.start_byte]
        wide = (-1, -1) if before.isascii() else _columns(before)
        out.append(Call(_text(name), _receiver(owner) if owner is not None else "", name.start_point[0] + 1,
                        name.start_point[1], caller, via, head, kind, arity, *wide, lines))
    out.sort(key=lambda call: (call.line, call.column))
    return out


def _is_member(node: Node) -> bool:
    """Whether a declaration sits in the body of a class, an `impl`, a trait or a module."""
    parent = node.parent
    if parent is not None and parent.type == "decorated_definition":
        parent = parent.parent
    if parent is not None and parent.type in ("block", "declaration_list", "class_body"):
        parent = parent.parent
    return parent is not None and parent.type in _TYPE_BODIES


def _function_bindings(node: Node) -> list[tuple[str, int, tuple[int, int]]]:
    """(name, where the name stands, the lines of the function) for each name this node binds to a function
    written there: `const f = () => …`, `f = lambda: …`, `f := func() {…}`, `let f = |x| …`."""
    names = node.child_by_field_name("name") or node.child_by_field_name("left") or node.child_by_field_name("pattern")
    values = node.child_by_field_name("value") or node.child_by_field_name("right")
    if names is None or values is None:
        return []
    pairs = zip(names.named_children if names.type == "expression_list" else [names],
                values.named_children if values.type == "expression_list" else [values])
    return [(_text(name), name.start_byte, (name.start_point[0] + 1, value.end_point[0] + 1))
            for name, value in pairs if name.type == "identifier" and value.type in _FUNCTION_VALUES]


def _columns(before: bytes) -> tuple[int, int]:
    """Where a name stands on its line, counted in UTF-16 units and in characters, given the bytes before it."""
    text = before.decode("utf-8", "replace")
    return len(text.encode("utf-16-le")) // 2, len(text)


def _innermost(found: list[Definition], positions: list[int]) -> list[int | None]:
    """For each position, the index of the innermost function or method it lies in. One sweep over both,
    sorted: a file of ten thousand definitions is no slower per call than one of ten."""
    spans = sorted((definition.start_byte, -definition.end_byte, index)
                   for index, definition in enumerate(found) if definition.kind in _CALLABLE)
    answer: list[int | None] = [None] * len(positions)
    open_spans: list[tuple[int, int]] = []     # (where it ends, its index), innermost last
    upcoming = 0
    for order in sorted(range(len(positions)), key=positions.__getitem__):
        position = positions[order]
        while upcoming < len(spans) and spans[upcoming][0] <= position:
            start, negative_end, index = spans[upcoming]
            while open_spans and open_spans[-1][0] <= start:
                open_spans.pop()
            open_spans.append((-negative_end, index))
            upcoming += 1
        while open_spans and open_spans[-1][0] <= position:
            open_spans.pop()
        answer[order] = open_spans[-1][1] if open_spans else None
    return answer


def _type_of(name: str, chain: tuple[int, ...], bound: dict[int, dict[str, set[int]]],
             stated: dict[int, dict[str, list[str]]], generic: set[str]) -> str:
    """The one type the scope that binds `name` states for it. '' when it states none, states two, binds
    the name again without saying (a loop over it, an unpacking), names a type parameter, or builds it
    with something the function binds itself (a factory passed in is not the file's function of that name)."""
    scope = next((scope for scope in reversed(chain) if name in bound[scope]), None)
    if scope is None:
        return ""
    said = stated[scope].get(name, [])
    if not said or not said[0] or len(set(said)) != 1 or len(said) < len(bound[scope][name] - {-1}):
        return ""
    kind = said[0]
    if kind in generic:
        return ""
    if kind.endswith("()"):
        builder = re.split(r"\.|::", kind[:-2])[0]
        if any(builder in bound[each] for each in chain):
            return ""
    return kind


def _head(start: Node, via: str, chain: tuple[int, ...], bound: dict[int, dict[str, set[int]]],
          receivers: dict[int, str]) -> tuple[str, str]:
    """What the first name of a call is bound to (see `Call.head`), and that name."""
    node = start
    while node.child_count:
        if node.type in _SEE_THROUGH and node.named_child_count == 1:
            node = node.named_children[0]
            continue
        if node.type in ("call", "call_expression", "method_invocation"):
            function = node.child_by_field_name("function") or node.child_by_field_name("name")
            return ("super" if function is not None and _text(function) == "super" else "value"), ""
        inner = next((node.child_by_field_name(field) for field in _OWNER_HEADS if node.child_by_field_name(field) is not None), None)
        if inner is None:
            return "value", ""
        node = inner
    kind, text = node.type, _text(node)
    if via == "path":
        return ("self" if text == "Self" else ""), text     # `self::f`, `super::f`, `crate::f` name modules, not objects
    if kind in ("this", "self"):
        return "self", text
    if kind == "super":
        return "super", text
    if "identifier" not in kind:
        return "value", ""
    for scope in reversed(chain):
        if text in bound[scope]:
            return ("self" if receivers.get(scope) == text else "local"), text
    return "", text


# Per language: the nodes that state a type for a local name.
_TYPED: dict[str, frozenset[str]] = {
    "python": frozenset({"typed_parameter", "typed_default_parameter", "assignment", "as_pattern"}),
    "typescript": frozenset({"required_parameter", "optional_parameter", "variable_declarator"}),
    "tsx": frozenset({"required_parameter", "optional_parameter", "variable_declarator"}),
    "javascript": frozenset({"variable_declarator"}),
    "go": frozenset({"parameter_declaration", "var_spec", "short_var_declaration"}),
    "rust": frozenset({"parameter", "let_declaration"}),
    "java": frozenset({"formal_parameter", "local_variable_declaration", "enhanced_for_statement"}),
    "cpp": frozenset({"parameter_declaration", "declaration"}),
    "c": frozenset({"parameter_declaration", "declaration"}),
}


def _stated(family: str, node: Node) -> list[tuple[str, str]]:
    """(name, type) for each local name whose type this node states: an annotation, or the constructor
    or call it is built from."""
    kind = node.type
    get = node.child_by_field_name
    if family == "python":
        if kind == "typed_parameter":
            name = node.named_children[0] if node.named_children else None
            return [(_text(name), named_type(_text(get("type"))))] if name is not None and name.type == "identifier" and get("type") else []
        if kind == "typed_default_parameter":
            return [(_text(get("name")), named_type(_text(get("type"))))] if get("name") and get("type") else []
        if kind == "assignment":
            left, right = get("left"), get("right")
            if left is None or left.type != "identifier":
                return []
            if get("type") is not None:
                return [(_text(left), named_type(_text(get("type"))))]
            if right is not None and right.type == "call" and right.child_by_field_name("function") is not None:
                return [(_text(left), constructed(_text(right.child_by_field_name("function"))))]
            return [(_text(left), "")] if right is not None else []
        value, alias = (node.named_children[0] if node.named_children else None), get("alias")      # with Session() as s
        target = alias.named_children[0] if alias is not None and alias.named_children else None
        if value is not None and value.type == "call" and target is not None and target.type == "identifier":
            return [(_text(target), constructed(_text(value.child_by_field_name("function"))))]
        return []
    if family in ("typescript", "tsx", "javascript"):
        name = get("pattern") if kind != "variable_declarator" else get("name")
        if name is None or name.type != "identifier":
            return []
        annotation, value = get("type"), get("value") if kind == "variable_declarator" else None
        if annotation is not None:
            inner = annotation.named_children[0] if annotation.type == "type_annotation" and annotation.named_children else annotation
            return [(_text(name), named_type(_text(inner)))]
        if value is not None and value.type == "new_expression" and value.child_by_field_name("constructor") is not None:
            return [(_text(name), named_type(_text(value.child_by_field_name("constructor"))))]
        return [(_text(name), "")] if kind == "variable_declarator" else []
    if family == "go":
        if kind == "short_var_declaration":
            left, right = get("left"), get("right")
            if left is None or right is None or left.named_child_count != 1 or right.named_child_count != 1:
                return [(_text(each), "") for each in (left.named_children if left is not None else []) if each.type == "identifier"]
            return [(_text(left.named_children[0]), _go_value(right.named_children[0]))] if left.named_children[0].type == "identifier" else []
        stated = named_type(_text(get("type"))) if get("type") is not None else ""
        if kind == "var_spec" and not stated:
            value = get("value")
            stated = _go_value(value.named_children[0]) if value is not None and value.named_child_count == 1 else ""
        return [(_text(name), stated) for name in node.children_by_field_name("name")]
    if family == "rust":
        pattern = get("pattern")
        if pattern is not None and pattern.type == "mut_pattern" and pattern.named_children:
            pattern = pattern.named_children[-1]
        if pattern is None or pattern.type != "identifier":
            return []
        if get("type") is not None:
            return [(_text(pattern), named_type(_text(get("type"))))]
        value = get("value") if kind == "let_declaration" else None
        if value is not None and value.type == "struct_expression":
            return [(_text(pattern), named_type(_text(value.child_by_field_name("name"))))]
        if value is not None and value.type == "call_expression" and value.child_by_field_name("function") is not None:
            return [(_text(pattern), constructed(_text(value.child_by_field_name("function"))))]
        return [(_text(pattern), "")] if kind == "let_declaration" else []
    if family == "java":
        stated = named_type(_text(get("type"))) if get("type") is not None else ""
        if kind != "local_variable_declaration":
            return [(_text(get("name")), stated)] if get("name") is not None else []
        out = []
        for declarator in node.children_by_field_name("declarator"):
            value = declarator.child_by_field_name("value")
            built = named_type(_text(value.child_by_field_name("type"))) if value is not None and value.type == "object_creation_expression" else ""
            out.append((_text(declarator.child_by_field_name("name")), built if stated == "var" else stated))
        return out
    stated = named_type(_text(get("type"))) if get("type") is not None else ""       # C and C++
    out = []
    for declarator in node.children_by_field_name("declarator"):
        name = declarator
        while name is not None and name.type != "identifier":
            name = name.child_by_field_name("declarator")
        if name is not None:
            out.append((_text(name), stated))
    return out


def _go_value(value: Node) -> str:
    """The type of what a Go expression builds, where the expression says it: `T{}`, `&T{}`, `new(T)`, `NewT()`."""
    if value.type == "unary_expression" and value.child_by_field_name("operand") is not None:
        value = value.child_by_field_name("operand")
    if value.type == "composite_literal":
        return named_type(_text(value.child_by_field_name("type")))
    if value.type == "call_expression":
        function, arguments = value.child_by_field_name("function"), value.child_by_field_name("arguments")
        if function is not None and _text(function) == "new" and arguments is not None and arguments.named_child_count == 1:
            return named_type(_text(arguments.named_children[0]))
        return constructed(_text(function)) if function is not None else ""
    return ""


def _receiver_name(family: str, scope: Node) -> str:
    """The name a method's own object goes by, in the languages that let the method choose it."""
    if family == "go" and scope.type == "method_declaration":
        receiver = scope.child_by_field_name("receiver")
        declared = next((child for child in receiver.named_children if child.type == "parameter_declaration"), None) if receiver else None
        name = declared.child_by_field_name("name") if declared is not None else None
        return _text(name) if name is not None else ""
    if family == "python" and scope.type == "function_definition":
        outer = scope.parent
        if outer is not None and outer.type == "decorated_definition":
            if any(child.type == "decorator" and "staticmethod" in _text(child) for child in outer.children):
                return ""
            outer = outer.parent
        if outer is None or outer.type != "block" or outer.parent is None or outer.parent.type != "class_definition":
            return ""
        parameters = scope.child_by_field_name("parameters")
        first = parameters.named_children[0] if parameters is not None and parameters.named_children else None
        return _text(first) if first is not None and first.type == "identifier" else ""
    return ""


def _bound_names(holder: Node, family: str) -> list[tuple[str, int]]:
    """The names a parameter list, an assignment target or a pattern binds, each with where it stands."""
    names: list[tuple[str, int]] = []
    stack = [holder]
    while stack:
        node = stack.pop()
        kind = node.type
        if kind in _BOUND_LEAVES:
            text = _text(node)
            if family != "rust" or not text[:1].isupper():   # `None`, `Ordering::Less`: a pattern names them, it does not bind them
                names.append((text, node.start_byte))
        elif kind == "identifier" or kind not in _PATTERNS and node is not holder:
            continue
        elif kind in ("pair_pattern", "field_pattern") and (node.child_by_field_name("value") or node.child_by_field_name("pattern")):
            stack.append(node.child_by_field_name("value") or node.child_by_field_name("pattern"))
        else:
            stack.extend(child for index, child in enumerate(node.children)
                         if child.is_named and node.field_name_for_child(index) not in _NOT_A_NAME)
    return names


def _is_require(node: Node) -> bool:
    """`const x = require('./m')`: an import, wherever it is written; the file's bindings record it."""
    if node.type != "variable_declarator":
        return False
    value = node.child_by_field_name("value")
    if value is not None and value.type == "member_expression":
        value = value.child_by_field_name("object")
    function = value.child_by_field_name("function") if value is not None and value.type == "call_expression" else None
    return function is not None and _text(function) == "require"


def _receiver(owner: Node) -> str:
    """What a call is made on, as written: on one line, and cut short when it is a long expression
    (a chain of builder calls over many lines says nothing more by being kept whole)."""
    text = " ".join(_text(owner).split())
    return text if len(text) <= RECEIVER_LIMIT else text[:RECEIVER_LIMIT - 1] + "…"


def _is_name(node: Node) -> bool:
    return node.child_count == 0 and "identifier" in node.type


def _split(callee: Node) -> tuple[Node, Node | None, str] | None:
    """The callee's own name, what it is called on, and how the two are joined; None when it has no name."""
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
        return node, None, ""
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
            if owner is None:
                return name, None, ""
            return name, owner, "path" if node.type in _PATHS else "member"
        return None
    return None


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")
