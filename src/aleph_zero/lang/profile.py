"""The language profile: node kinds and field names for one grammar, as data.

A profile is the only place language-specific knowledge lives. The walkers that read
it (`definitions`, `imports`) never name a grammar. Profiles are node-kind tables
rather than tree-sitter queries because the distinctions that matter are structural:
a decorator or `export` wraps the definition that is the real entity, a Go method
names its type outside any class body, a C++ name sits at the end of a declarator chain.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Wrapper:
    """A node that wraps the definition which is the real entity: Python's
    `decorated_definition`, TypeScript's `export_statement`, a C++ `template_declaration`.
    The entity is the inner definition; the wrapper contributes its lines."""

    kind: str
    #: Field holding the wrapped definition, or None to take the first definition child.
    field_name: str | None = None


@dataclass(frozen=True)
class ImportSpec:
    """How one language spells an import. Styles, because import syntax clusters into
    families: `python`, `ecmascript`, `go`, `rust`, `java`, `include`."""

    style: str = "python"
    #: Statement kinds that introduce a dependency.
    statement_kinds: frozenset[str] = frozenset()
    #: Field holding the module or specifier.
    module_field: str = "module_name"
    #: Field holding imported names (python style).
    name_field: str = "name"
    #: Node kinds for a relative module reference (`from ..pkg import x`).
    relative_kinds: frozenset[str] = frozenset()
    #: Node kinds marking `from x import *`.
    wildcard_kinds: frozenset[str] = frozenset()
    #: Node kinds for `x as y`.
    alias_kinds: frozenset[str] = frozenset()
    #: Kinds whose text is a quoted specifier needing the quotes stripped.
    string_kinds: frozenset[str] = frozenset()
    #: Token that marks a type-only import, which creates no runtime coupling.
    type_only_token: str | None = None
    #: Callee names that import dynamically: `importlib.import_module`, `require`.
    dynamic_callees: frozenset[str] = frozenset()
    #: Names that, assigned to, publish a module's interface: `module.exports`, `exports`.
    reexport_targets: frozenset[str] = frozenset()
    #: Call node kinds to inspect for dynamic imports.
    call_kinds: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LanguageProfile:
    """What is known about one language's syntax."""

    name: str
    grammar: str
    extensions: frozenset[str]
    #: Nodes that declare a named callable.
    function_like: frozenset[str]
    #: Nodes that declare a type or a scope, and what they are: 'class', 'interface',
    #: 'enum', 'type' (an alias), 'namespace', or 'impl' (a block that only lends its
    #: name to the members inside it: Rust's `impl Service { ... }`).
    types: Mapping[str, str]
    #: Nodes that declare a value, and what they are by default: 'constant', 'variable',
    #: 'field' or 'macro'.
    values: Mapping[str, str] = field(default_factory=dict)
    #: Callables that have no name of their own. One is a definition only when bound to
    #: a name: `const handler = () => {}`, `var Fn = func() {}`.
    anonymous: frozenset[str] = frozenset()
    #: Nodes that wrap a definition. Keyed by node kind.
    wrappers: Mapping[str, Wrapper] = field(default_factory=dict)
    #: Field holding an entity's identifier.
    name_field: str = "name"
    #: Field holding an entity's body.
    body_field: str = "body"
    #: Field naming the type a callable is a method of, where the grammar puts it outside
    #: any class body. Go: `func (c *Counter) Inc()`.
    receiver_field: str = ""
    #: Field naming the type an 'impl' block belongs to. Rust: `impl Display for Service`.
    implements_field: str = ""
    #: Where a type node's kind depends on the kind of one of its children: Go's
    #: `type_spec` is a class when it holds a `struct_type`, an interface when it holds
    #: an `interface_type`, and an alias otherwise.
    type_refinement: Mapping[str, str] = field(default_factory=dict)
    #: Statements that hold one declaration and nothing else, whose lines belong to it:
    #: `type X struct{}` around a Go type_spec, `export const f = ...` around a declarator.
    holders: frozenset[str] = frozenset()
    #: Object literals, and the key-value entries inside them. A function that is the value
    #: of a key is called by that key: `const api = { get() {}, post: () => {} }`.
    object_kinds: frozenset[str] = frozenset()
    pair_kinds: frozenset[str] = frozenset()
    #: True where a name is reached by following `declarator` fields: C and C++.
    declarator_chain: bool = False
    #: Tokens that make a value a constant: `const`, `constexpr`, `readonly`.
    constant_tokens: frozenset[str] = frozenset()
    #: True where a bare string that opens a body is its documentation: Python.
    docstrings: bool = False
    comment_kinds: frozenset[str] = frozenset()
    string_kinds: frozenset[str] = frozenset()
    imports: ImportSpec = field(default_factory=ImportSpec)

    def is_definition(self, kind: str) -> bool:
        return kind in self.function_like or kind in self.types or kind in self.anonymous
