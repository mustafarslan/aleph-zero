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
