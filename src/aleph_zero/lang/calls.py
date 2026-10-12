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
    receivers: dict[int, str] = {}      # scope -> the name its method's receiver goes by, where the language names it
    seen: list[tuple[Node, Node | None, str, tuple[int, ...]]] = []
    stack: list[tuple[Node, tuple[int, ...]]] = [(tree.root_node, ())]
    while stack:  # explicit: expressions nest deeper than Python recurses
        node, chain = stack.pop()
        kind = node.type
        if chain and kind in _NAMED_OUTSIDE:
            name = node.child_by_field_name("name")
            if name is not None:
                bound[chain[-1]].add(_text(name))
        if kind in scopes:
            chain = (*chain, node.id)
            bound[node.id] = set()
            receiver = _receiver_name(family, node)
            if receiver:
                receivers[node.id] = receiver
                bound[node.id].add(receiver)
        if chain and kind in binders and not _is_require(node):
            for field in binders[kind]:
                for holder in ([node] if not field else node.children_by_field_name(field)):
                    bound[chain[-1]].update(_bound_names(holder, family))
        stack.extend((child, chain) for child in reversed(node.children))
        field = sites.get(kind)
        if field is None:
            continue
        callee = node.child_by_field_name(field)
        if callee is None:
            continue
        owner = node.child_by_field_name("object") if family == "java" and kind == "method_invocation" else None
        split = (callee, owner, "member" if owner is not None else "") if owner is not None or _is_name(callee) else _split(callee)
        if split is not None:
            seen.append((*split, chain))
    out: list[Call] = []
    for name, owner, via, chain in seen:
        position = name.start_byte
        caller = next((index for _, start, end, index in callables if start <= position < end), None)
        out.append(Call(_text(name), _text(owner) if owner is not None else "", name.start_point[0] + 1,
                        name.start_point[1], caller, via, _head(name if owner is None else owner, via, chain, bound, receivers)))
    out.sort(key=lambda call: (call.line, call.column))
    return out


def _head(start: Node, via: str, chain: tuple[int, ...], bound: dict[int, set[str]], receivers: dict[int, str]) -> str:
    """What the first name of a call is bound to: see `Call.head`."""
    node = start
    while node.child_count:
        if node.type in _SEE_THROUGH and node.named_child_count == 1:
            node = node.named_children[0]
            continue
        if node.type in ("call", "call_expression", "method_invocation"):
            function = node.child_by_field_name("function") or node.child_by_field_name("name")
            return "super" if function is not None and _text(function) == "super" else "value"
        inner = next((node.child_by_field_name(field) for field in _OWNER_HEADS if node.child_by_field_name(field) is not None), None)
        if inner is None:
            return "value"
        node = inner
    kind, text = node.type, _text(node)
    if via == "path":
        return "self" if text == "Self" else ""     # `self::f`, `super::f`, `crate::f` name modules, not objects
    if kind in ("this", "self"):
        return "self"
    if kind == "super":
        return "super"
    if "identifier" not in kind:
        return "value"
    for scope in reversed(chain):
        if text in bound[scope]:
            return "self" if receivers.get(scope) == text else "local"
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
