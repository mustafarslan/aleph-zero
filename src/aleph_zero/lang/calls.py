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

from dataclasses import dataclass

from tree_sitter import Node, Tree

from . import query
from .bindings import constructed, named_type
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
_PARTS = {"named": 0, "scope": 1, "binds": 2, "typed": 3, "call": 4}
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
    #                       '' nothing local: a name of the file, an import, a builtin
    receiver_type: str = ""  # for a call on a local name: the type its function declares for it (`generic.Gauge`), or
    #                       the call that built it (`Engine()`); '' when the function does not say


def calls(profile: LanguageProfile, tree: Tree, data: bytes, found: list[Definition]) -> list[Call]:
    """Every named call in the file, in source order."""
    family = profile.name
    sites = _SITES.get(family)
    if not sites:
        return []
    callables = sorted((definition.end_byte - definition.start_byte, definition.start_byte, definition.end_byte, index)
                       for index, definition in enumerate(found) if definition.kind in _CALLABLE)
    scopes, binders = _SCOPES.get(family, frozenset()), _BINDERS.get(family, {})
    bound: dict[int, set[str]] = {}     # scope -> the names bound in it
    declared: dict[int, dict[str, str]] = {}  # scope -> name -> the type the scope states for it ('' when it states two)
    receivers: dict[int, str] = {}      # scope -> the name its method's receiver goes by, where the language names it
    seen: list[tuple[Node, Node | None, str, tuple[int, ...]]] = []
    # Only the nodes that matter are visited, found by the parser's query engine: scopes, what binds or
    # types a local name, and calls. In source order, an outer node first, so a scope is open before what
    # it holds is met; one node can play several parts, taken in the order of _PARTS.
    parts = query.by_role(family, tree, {"named": _NAMED_OUTSIDE, "scope": scopes, "binds": binders,
                                         "typed": _TYPED.get(family, frozenset()), "call": sites})
    events = sorted(((node.start_byte, -node.end_byte, _PARTS[part], node) for part, nodes in parts.items() for node in nodes),
                    key=lambda event: event[:3])
    open_scopes: list[tuple[int, int]] = []    # (where it ends, its id), innermost last
    for start, _, part, node in events:
        while open_scopes and open_scopes[-1][0] <= start:
            open_scopes.pop()
        if part == 0:           # a declaration whose own name belongs to the scope around it
            name = node.child_by_field_name("name")
            if open_scopes and name is not None:
                bound[open_scopes[-1][1]].add(_text(name))
        elif part == 1:         # a scope opens
            open_scopes.append((node.end_byte, node.id))
            bound[node.id], declared[node.id] = set(), {}
            receiver = _receiver_name(family, node)
            if receiver:
                receivers[node.id] = receiver
                bound[node.id].add(receiver)
        elif part == 2:
            if open_scopes and not _is_require(node):
                for field in binders[node.type]:
                    for holder in ([node] if not field else node.children_by_field_name(field)):
                        bound[open_scopes[-1][1]].update(_bound_names(holder, family))
        elif part == 3:
            if open_scopes:
                known = declared[open_scopes[-1][1]]
                for name, stated in _stated(family, node):
                    known[name] = stated if known.get(name, stated) == stated else ""
        else:
            callee = node.child_by_field_name(sites[node.type])
            if callee is None:
                continue
            owner = node.child_by_field_name("object") if family == "java" and node.type == "method_invocation" else None
            split = (callee, owner, "member" if owner is not None else "") if owner is not None or _is_name(callee) else _split(callee)
            if split is not None:
                seen.append((*split, tuple(scope for _, scope in open_scopes)))
    out: list[Call] = []
    for name, owner, via, chain in seen:
        position = name.start_byte
        caller = next((index for _, start, end, index in callables if start <= position < end), None)
        head, first = _head(name if owner is None else owner, via, chain, bound, receivers)
        stated = next((declared[scope].get(first, "") for scope in reversed(chain) if first in bound[scope]), "") \
            if head == "local" and owner is not None else ""
        out.append(Call(_text(name), _receiver(owner) if owner is not None else "", name.start_point[0] + 1,
                        name.start_point[1], caller, via, head, stated))
    out.sort(key=lambda call: (call.line, call.column))
    return out


def _head(start: Node, via: str, chain: tuple[int, ...], bound: dict[int, set[str]], receivers: dict[int, str]) -> tuple[str, str]:
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


def _bound_names(holder: Node, family: str) -> set[str]:
    """The names a parameter list, an assignment target or a pattern binds."""
    names: set[str] = set()
    stack = [holder]
    while stack:
        node = stack.pop()
        kind = node.type
        if kind in _BOUND_LEAVES:
            text = _text(node)
            if family != "rust" or not text[:1].isupper():   # `None`, `Ordering::Less`: a pattern names them, it does not bind them
                names.add(text)
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
