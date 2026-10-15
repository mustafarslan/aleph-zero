from pathlib import Path

import pytest
import tree_sitter_language_pack as pack

from aleph_zero import lang
from aleph_zero.lang import parse
from aleph_zero.lang.definitions import definitions

SAMPLES = Path(__file__).parent / "samples"


def found(name, source=None, file=None):
    profile = lang.PROFILES[name]
    data = (SAMPLES / file).read_bytes() if file else source.encode()
    return definitions(profile, parse.tree(profile, data), data)


def listing(defs):
    return [(d.kind, d.qualified) for d in defs]


def one(defs, qualified, kind=None):
    matches = [d for d in defs if d.qualified == qualified and (kind is None or d.kind == kind)]
    assert len(matches) == 1, (qualified, listing(defs))
    return matches[0]


def test_python_definitions():
    defs = found("python", file="sample.py")
    assert listing(defs) == [
        ("constant", "MAX_RETRY"), ("variable", "_cache"), ("function", "refresh_token"),
        ("class", "TokenStore"), ("field", "TokenStore.ttl"), ("method", "TokenStore.__init__"),
        ("method", "TokenStore.size"), ("method", "TokenStore.make"),
    ]
    refresh = one(defs, "refresh_token")
    assert refresh.signature == "def refresh_token(session: Session, *, force: bool = False) -> str"
    assert refresh.doc == "Exchange a refresh token for a new access token."
    assert (refresh.start_line, refresh.end_line) == (10, 12)
    assert one(defs, "TokenStore").doc == "Keeps tokens."
    assert one(defs, "TokenStore").signature == "class TokenStore(Base)"
    size = one(defs, "TokenStore.size")
    assert (size.start_line, size.end_line) == (23, 25)  # the decorator belongs to it
    assert size.signature == "@property def size(self) -> int"
    assert defs[one(defs, "TokenStore.ttl").parent].qualified == "TokenStore"
    assert one(defs, "MAX_RETRY").signature == "MAX_RETRY = 6"


def test_typescript_definitions():
    defs = found("typescript", file="sample.ts")
    assert listing(defs) == [
        ("constant", "MAX_RETRY"), ("variable", "cache"), ("function", "refreshToken"), ("function", "handler"),
        ("interface", "Store"), ("method", "Store.get"), ("field", "Store.size"),
        ("type", "Id"), ("enum", "Color"), ("class", "TokenStore"),
        ("field", "TokenStore.ttl"), ("field", "TokenStore.instances"), ("method", "TokenStore.constructor"),
        ("method", "TokenStore.size"), ("method", "TokenStore.get"), ("method", "TokenStore.flush"),
    ]
    refresh = one(defs, "refreshToken")
    assert refresh.doc == "Exchange a refresh token."
    assert refresh.start_line == 8  # the doc comment belongs to it
    assert refresh.signature == "export async function refreshToken(session: Session, force = false): Promise<string>"
    assert one(defs, "handler").signature == "export const handler = async (req: Request): Promise<Response>"
    assert one(defs, "TokenStore").signature == "export abstract class TokenStore extends Base implements Store"
    assert one(defs, "TokenStore.size").doc == "Number of tokens."


def test_javascript_definitions():
    defs = found("javascript", file="sample.js")
    assert listing(defs) == [
        ("function", "add"), ("function", "legacy"), ("class", "Shelf"),
        ("field", "Shelf.count"), ("method", "Shelf.#secret"), ("method", "Shelf.items"),
    ]


def test_go_definitions():
    defs = found("go", file="sample.go")
    assert listing(defs) == [
        ("constant", "MaxRetry"), ("variable", "cache"), ("class", "TokenStore"),
        ("field", "TokenStore.Path"), ("field", "TokenStore.ttl"), ("interface", "Store"), ("method", "Store.Get"),
        ("method", "TokenStore.Get"), ("function", "RefreshToken"),
    ]
    get = one(defs, "TokenStore.Get")
    assert get.signature == "func (s *TokenStore) Get(key string) (string, bool)"
    assert get.doc == "Get returns a token."
    assert one(defs, "TokenStore").signature == "type TokenStore struct"
    assert one(defs, "TokenStore").doc == "TokenStore keeps tokens."
    grouped = found("go", "package p\n\nconst (\n\tA = 1\n\tB = 2\n)\n\nvar x, y int\n\ntype ID = string\n")
    assert listing(grouped) == [("constant", "A"), ("constant", "B"), ("variable", "x"), ("variable", "y"), ("type", "ID")]


def test_rust_definitions():
    defs = found("rust", file="sample.rs")
    assert listing(defs) == [
        ("constant", "MAX_RETRY"), ("constant", "CACHE"), ("class", "TokenStore"),
        ("field", "TokenStore.path"), ("field", "TokenStore.ttl"), ("enum", "Color"),
        ("interface", "Store"), ("method", "Store.get"), ("method", "TokenStore.get"), ("method", "TokenStore.new"),
        ("function", "refresh_token"), ("namespace", "inner"), ("function", "inner.helper"),
    ]
    assert one(defs, "TokenStore").doc == "Keeps tokens."
    assert one(defs, "TokenStore.get").doc == "Returns a token."
    assert one(defs, "TokenStore.new").signature == "pub fn new(path: String) -> Self"
    generic = found("rust", "impl<'a, T> Reader<'a, T> {\n    fn read(&self) {}\n}\n")
    assert listing(generic) == [("method", "Reader.read")]


def test_java_definitions():
    defs = found("java", file="Sample.java")
    assert listing(defs) == [
        ("class", "TokenStore"), ("constant", "TokenStore.MAX_RETRY"), ("field", "TokenStore.cache"),
        ("method", "TokenStore.TokenStore"), ("method", "TokenStore.get"),
        ("interface", "TokenStore.Store"), ("method", "TokenStore.Store.get"),
        ("enum", "TokenStore.Color"), ("class", "TokenStore.Pair"),
    ]
    get = one(defs, "TokenStore.get")
    assert get.doc == "Returns a token."
    assert get.signature == "@Override public String get(String key)"
    assert (get.start_line, get.end_line) == (14, 16)
    assert one(defs, "TokenStore").doc == "Keeps tokens."


def test_cpp_definitions():
    defs = found("cpp", file="sample.cpp")
    assert listing(defs) == [
        ("namespace", "auth"), ("constant", "auth.kMaxRetry"), ("variable", "auth.g_cache"),
        ("class", "auth.TokenStore"), ("method", "auth.TokenStore.TokenStore"), ("method", "auth.TokenStore.~TokenStore"),
        ("method", "auth.TokenStore.Get"), ("field", "auth.TokenStore.instances"), ("field", "auth.TokenStore.ttl_"),
        ("class", "auth.Pair"), ("field", "auth.Pair.a"), ("field", "auth.Pair.b"), ("enum", "auth.Color"),
        ("function", "auth.Identity"), ("method", "auth.TokenStore.Get"), ("function", "auth.RefreshToken"),
        ("macro", "MAX_BUF"), ("type", "ulong_t"), ("type", "Id"),
    ]
    assert one(defs, "auth.TokenStore").doc == "Keeps tokens."
    assert one(defs, "auth.Identity").signature == "template <typename T> T Identity(T value)"
    out_of_class = [d for d in defs if d.qualified == "auth.TokenStore.Get"][1]
    assert out_of_class.signature == "std::string TokenStore::Get(const std::string& key) const"
    assert (out_of_class.start_line, out_of_class.end_line) == (26, 29)
    assert one(defs, "auth.RefreshToken").signature == "int RefreshToken(Session* session, bool force)"


def test_c_definitions():
    defs = found("c", file="sample.c")
    assert listing(defs) == [
        ("macro", "SQUARE"), ("macro", "LIMIT"), ("type", "Pair"), ("field", "Pair.a"), ("field", "Pair.b"),
        ("class", "Node"), ("field", "Node.next"), ("field", "Node.value"),
        ("variable", "counter"), ("constant", "NAME"), ("function", "add"), ("function", "lookup"),
    ]
    assert one(defs, "add").doc == "Adds two numbers."


def test_a_function_inside_a_function_is_part_of_its_parent():
    defs = found("python", "def outer():\n    def inner():\n        return 1\n    helper = lambda: 2\n    return inner\n")
    assert listing(defs) == [("function", "outer")]


def test_a_name_bound_to_an_anonymous_function_is_a_function():
    assert listing(found("python", "double = lambda x: x * 2\n")) == [("function", "double")]
    assert listing(found("go", "package p\n\nvar Fn = func() int { return 1 }\n")) == [("function", "Fn")]
    assert listing(found("typescript", "const run = function* () { yield 1 };\n")) == [("function", "run")]


def test_an_unbound_anonymous_function_is_not_a_definition():
    assert listing(found("typescript", "items.map((x) => x + 1);\nsetTimeout(function () {}, 5);\n")) == []


def test_a_syntax_error_does_not_hide_the_definitions_around_it():
    source = "def before():\n    return 1\n\nvalue = = 3\nclass :\n\ndef after():\n    return 3\n"
    names = [d.qualified for d in found("python", source)]
    assert "before" in names and "after" in names
    # An unclosed bracket swallows the rest of the file in the parser; what parsed is still returned.
    swallowed = "def before():\n    return 1\n\ndef broken(:\n    return ((\n\ndef after():\n    return 3\n"
    assert "before" in [d.qualified for d in found("python", swallowed)]


def test_deeply_nested_expressions_do_not_exhaust_the_stack():
    source = "TOTAL = " + "(" * 400 + "1" + ")" * 400 + "\n\ndef after():\n    return 1\n"
    assert ("function", "after") in listing(found("python", source))


def test_definitions_in_conditional_blocks_are_found():
    source = "try:\n    import fast\nexcept ImportError:\n    def fallback():\n        return 1\n"
    assert listing(found("python", source)) == [("function", "fallback")]


def test_forward_declarations_and_uses_of_a_type_are_not_definitions():
    defs = found("cpp", "class Later;\nstruct Pair p;\nenum Color c;\nclass Real { int x; };\n")
    assert listing(defs) == [("variable", "p"), ("variable", "c"), ("class", "Real"), ("field", "Real.x")]


def test_typescript_overloads_namespaces_and_default_exports():
    source = (
        "export function pick(a: string): string;\nexport function pick(a: number): number;\n"
        "export function pick(a: any) { return a; }\n"
        "declare namespace NS { function inside(): void }\n"
        "export default function main() {}\n"
    )
    assert listing(found("typescript", source)) == [
        ("function", "pick"), ("function", "pick"), ("function", "pick"),
        ("namespace", "NS"), ("function", "NS.inside"), ("function", "main"),
    ]


@pytest.mark.parametrize("name,file", [("python", "sample.py"), ("typescript", "sample.ts"), ("go", "sample.go"),
                                       ("rust", "sample.rs"), ("java", "Sample.java")])
def test_everything_the_language_pack_finds_is_found(name, file):
    # The pack's own extraction is the baseline to beat (ADR-007).
    source = (SAMPLES / file).read_text()
    result = pack.process(source, pack.ProcessConfig(language=name, structure=True, symbols=True))

    def names(items):
        for item in items:
            if item.name and str(item.kind) not in ("Module", "StructureKind.Module"):
                yield item.name
            yield from names(getattr(item, "children", []))

    theirs = set(names(result.structure)) | {symbol.name for symbol in result.symbols}
    theirs.discard("com.example.auth")  # a package clause, not a definition
    ours = {d.name for d in found(name, file=file)}
    assert theirs - ours == set()
    assert len(ours) > len(theirs)


def test_an_export_macro_in_front_of_a_declaration_does_not_become_part_of_the_name():
    source = (
        "GGML_API ggml_backend_t ggml_backend_cuda_init(void);\n"
        "XXH_PUBLIC_API XXH_errorcode\nXXH64_update(XXH64_state_t* state, const void* input);\n"
        "GGML_API struct ggml_tensor * ggml_new(struct ggml_context * ctx);\n"
        "static inline LLAMA_API int32_t llama_n(const struct llama_model * model) { return 1; }\n"
    )
    # Headers are read with the C++ grammar, which recovers all four.
    assert listing(found("cpp", source)) == [
        ("function", "ggml_backend_cuda_init"), ("function", "XXH64_update"),
        ("function", "ggml_new"), ("function", "llama_n"),
    ]
    # The C grammar loses a prototype behind an unknown macro when the return type is a
    # plain name (it reads `MACRO type` as a variable and the rest as a call). Known limit.
    in_c = [d.qualified for d in found("c", source) if d.kind == "function"]
    assert in_c == ["XXH64_update", "ggml_new", "llama_n"]


def test_cpp_callables_behind_references_pointers_and_conversions_are_found():
    source = (
        "class Cfg {\n public:\n"
        "  const std::string& name() const { return name_; }\n"
        "  Cfg& operator=(const Cfg&) = default;\n"
        "  int& at(int index);\n"
        "  explicit operator bool() const;\n"
        "  int operator[](int index) const { return index; }\n"
        "  void (*callback)(int);\n"
        " private:\n  std::string name_;\n};\n"
        "Cfg& global_cfg() { static Cfg cfg; return cfg; }\n"
        "int (*resolve(int code))(int);\n"
    )
    assert listing(found("cpp", source)) == [
        ("class", "Cfg"), ("method", "Cfg.name"), ("method", "Cfg.operator="), ("method", "Cfg.at"),
        ("method", "Cfg.operator bool"), ("method", "Cfg.operator[]"), ("field", "Cfg.callback"),
        ("field", "Cfg.name_"), ("function", "global_cfg"), ("function", "resolve"),
    ]


def test_members_of_an_unnamed_class_are_still_methods():
    defs = found("typescript", "export default class {\n  start() { return 1; }\n}\n")
    assert listing(defs) == [("method", "start")]


def test_dotted_and_nested_namespace_names_keep_every_part():
    assert listing(found("typescript", "namespace App.Core {\n  export function boot() {}\n}\n")) == [
        ("namespace", "App.Core"), ("function", "App.Core.boot")]
    assert listing(found("cpp", "namespace a::b {\nint f() { return 1; }\n}\n")) == [
        ("namespace", "a.b"), ("function", "a.b.f")]


def test_a_function_expression_is_known_by_the_name_it_is_bound_to():
    assert listing(found("javascript", "var visible = function inner() { return 1; };\n")) == [("function", "visible")]


def test_a_licence_header_is_not_the_first_definitions_documentation():
    source = "// Copyright (c) 2026 Example Corp.\n// SPDX-License-Identifier: MIT\nexport function first() {}\n"
    first, = found("typescript", source)
    assert first.doc == "" and first.start_line == 3
    documented, = found("typescript", "// Starts the service.\nexport function first() {}\n")
    assert documented.doc == "Starts the service." and documented.start_line == 1


# ---- carried from M1b to M3 -------------------------------------------------------------------------------

def test_methods_of_an_object_literal_bound_to_a_name_are_definitions():
    defs = found("javascript", """
const api = {
  // Fetch one record.
  get(id) { return fetch(id); },
  post: function (body) { return send(body); },
  remove: (id) => drop(id),
  limit: 10,
  nested: {
    deep() { return 1; },
  },
};
""")
    assert one(defs, "api").kind == "constant"
    for name in ("api.get", "api.post", "api.remove", "api.nested.deep"):
        assert one(defs, name).kind == "method"
    assert one(defs, "api.get").doc == "Fetch one record."
    assert one(defs, "api.post").signature.startswith("post: function (body)")
    assert (one(defs, "api.remove").start_line, one(defs, "api.remove").end_line) == (6, 6)
    assert one(defs, "api.get").parent == defs.index(one(defs, "api"))
    assert not any(d.qualified in ("api.limit", "limit") for d in defs)   # plain values are not listed one by one


def test_typescript_object_literal_methods_and_exported_objects():
    defs = found("typescript", """
export const routes = {
  list(req: Request): Response { return ok(); },
  create: async (req: Request) => { return ok(); },
};
""")
    assert one(defs, "routes.list").kind == "method" and one(defs, "routes.create").kind == "method"
    assert "list(req: Request): Response" in one(defs, "routes.list").signature


def test_a_function_valued_key_in_an_unnamed_object_is_found_by_its_key():
    """`describe`-style option objects and component definitions: the key is the only
    name the function has."""
    defs = found("javascript", """
export default {
  methods: {
    fetchData() { return 1; },
    reload: async () => { return 2; },
  },
};
""")
    names = {d.name for d in defs}
    assert {"fetchData", "reload"} <= names


def test_every_target_of_a_chained_assignment_is_defined():
    defs = found("python", """
first = second = lambda value: value + 1
alpha = beta = 3
MAX_A = MAX_B = 10
handler = lambda event: event
""")
    assert one(defs, "first").kind == "function" and one(defs, "second").kind == "function"
    assert one(defs, "alpha").kind == "variable" and one(defs, "beta").kind == "variable"
    assert one(defs, "MAX_A").kind == "constant" and one(defs, "MAX_B").kind == "constant"
    assert one(defs, "handler").kind == "function"
    assert one(defs, "second").signature.startswith("first = second = lambda value")


def test_c_declarations_behind_an_unknown_macro_are_found_in_a_c_file():
    defs = found("c", """
EXPORT_API int widget_open(struct widget *w, int flags);
STATIC_INLINE int widget_close(struct widget *w)
{
    return 0;
}
""")
    assert one(defs, "widget_open").kind == "function" and one(defs, "widget_close").kind == "function"
    assert (one(defs, "widget_close").start_line, one(defs, "widget_close").end_line) == (3, 6)


def test_functions_in_an_object_passed_to_a_call_are_not_listed_by_their_keys():
    """Hooks of a test framework and option objects of calls: every test file would define
    `beforeEach`. Objects that are bound, assigned or exported are another matter."""
    defs = found("javascript", """
QUnit.module('admin', {
  beforeEach: function () { setup(); },
  nested: { afterEach: () => teardown() },
});
app.use({ handler: (req) => req });
module.exports = {
  exported: function () { return 1; },
};
""")
    names = {d.name for d in defs}
    assert not {"beforeEach", "afterEach", "handler"} & names
    assert "exported" in names


def test_with_inner_functions_the_definitions_outside_bodies_are_exactly_those_of_the_walk_without(tmp_path):
    # What the chunker, the vectors and the code graph read must not change: on every language's sample, leaving out
    # what is nested gives the old walk back, field for field. Nested ones sit under a function, qualified by it.
    from pathlib import Path

    from aleph_zero import filters, lang
    from aleph_zero.lang import parse
    from aleph_zero.lang.definitions import definitions

    samples = Path(__file__).parent / "samples"
    for sample in sorted(samples.iterdir()):
        profile = lang.PROFILES.get(filters.family(sample.name))
        if profile is None:
            continue
        data = sample.read_bytes()
        tree = parse.tree(profile, data)
        before = definitions(profile, tree, data)
        after = definitions(profile, tree, data, inner=True)
        top = [d for d in after if not d.nested]
        assert [(d.kind, d.qualified, d.start_line, d.end_line, d.signature) for d in top] == \
               [(d.kind, d.qualified, d.start_line, d.end_line, d.signature) for d in before], sample.name
        for d in after:
            if d.nested:
                holder = after[d.parent]
                assert d.qualified.startswith(holder.qualified + ".") and holder.start_line <= d.start_line <= holder.end_line
