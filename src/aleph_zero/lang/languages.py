"""The profiles. Every node kind below is exercised by a test that parses real source."""
from __future__ import annotations

from dataclasses import replace

from .profile import ImportSpec, LanguageProfile, Wrapper

PYTHON = LanguageProfile(
    name="python",
    grammar="python",
    extensions=frozenset({".py", ".pyi"}),
    function_like=frozenset({"function_definition"}),
    anonymous=frozenset({"lambda"}),
    types={"class_definition": "class"},
    values={"assignment": "variable"},
    wrappers={"decorated_definition": Wrapper("decorated_definition", "definition")},
    docstrings=True,
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string", "concatenated_string"}),
    imports=ImportSpec(
        style="python",
        statement_kinds=frozenset({"import_statement", "import_from_statement"}),
        module_field="module_name",
        name_field="name",
        relative_kinds=frozenset({"relative_import"}),
        wildcard_kinds=frozenset({"wildcard_import"}),
        alias_kinds=frozenset({"aliased_import"}),
        string_kinds=frozenset({"string"}),
        dynamic_callees=frozenset({"importlib.import_module", "__import__"}),
        call_kinds=frozenset({"call"}),
    ),
)

_ECMASCRIPT_IMPORTS = ImportSpec(
    style="ecmascript",
    statement_kinds=frozenset({"import_statement", "export_statement"}),
    module_field="source",
    string_kinds=frozenset({"string"}),
    type_only_token="type",
    dynamic_callees=frozenset({"require", "import"}),
    reexport_targets=frozenset({"module.exports", "exports"}),
    call_kinds=frozenset({"call_expression"}),
)

TYPESCRIPT = LanguageProfile(
    name="typescript",
    grammar="typescript",
    extensions=frozenset({".ts", ".mts", ".cts"}),
    function_like=frozenset({
        "function_declaration", "generator_function_declaration", "function_signature",
        "method_definition", "method_signature", "abstract_method_signature",
    }),
    anonymous=frozenset({"function_expression", "generator_function", "arrow_function"}),
    types={
        "class_declaration": "class", "abstract_class_declaration": "class", "class": "class",
        "interface_declaration": "interface", "type_alias_declaration": "type",
        "enum_declaration": "enum", "internal_module": "namespace", "module": "namespace",
    },
    values={"variable_declarator": "variable", "public_field_definition": "field", "property_signature": "field"},
    wrappers={"export_statement": Wrapper("export_statement", "declaration")},
    object_kinds=frozenset({"object"}),
    pair_kinds=frozenset({"pair"}),
    argument_kinds=frozenset({"arguments"}),
    holders=frozenset({"lexical_declaration", "variable_declaration", "export_statement", "ambient_declaration"}),
    constant_tokens=frozenset({"const", "readonly"}),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string", "template_string"}),
    imports=_ECMASCRIPT_IMPORTS,
)

TSX = replace(TYPESCRIPT, name="tsx", grammar="tsx", extensions=frozenset({".tsx"}))

JAVASCRIPT = LanguageProfile(
    name="javascript",
    grammar="javascript",
    extensions=frozenset({".js", ".jsx", ".mjs", ".cjs"}),
    function_like=frozenset({"function_declaration", "generator_function_declaration", "method_definition"}),
    anonymous=frozenset({"function_expression", "generator_function", "arrow_function"}),
    types={"class_declaration": "class", "class": "class"},
    values={"variable_declarator": "variable", "field_definition": "field"},
    wrappers={"export_statement": Wrapper("export_statement", "declaration")},
    object_kinds=frozenset({"object"}),
    pair_kinds=frozenset({"pair"}),
    argument_kinds=frozenset({"arguments"}),
    holders=frozenset({"lexical_declaration", "variable_declaration", "export_statement"}),
    constant_tokens=frozenset({"const"}),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string", "template_string"}),
    imports=replace(_ECMASCRIPT_IMPORTS, type_only_token=None),
)

GO = LanguageProfile(
    name="go",
    grammar="go",
    extensions=frozenset({".go"}),
    function_like=frozenset({"function_declaration", "method_declaration", "method_elem"}),
    anonymous=frozenset({"func_literal"}),
    types={"type_spec": "type", "type_alias": "type"},
    type_refinement={"struct_type": "class", "interface_type": "interface"},
    values={"const_spec": "constant", "var_spec": "variable", "field_declaration": "field"},
    receiver_field="receiver",
    holders=frozenset({"type_declaration", "const_declaration", "var_declaration"}),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"interpreted_string_literal", "raw_string_literal"}),
    imports=ImportSpec(
        style="go",
        statement_kinds=frozenset({"import_declaration"}),
        module_field="path",
        name_field="name",
        string_kinds=frozenset({"interpreted_string_literal", "raw_string_literal"}),
        call_kinds=frozenset({"call_expression"}),
    ),
)

RUST = LanguageProfile(
    name="rust",
    grammar="rust",
    extensions=frozenset({".rs"}),
    function_like=frozenset({"function_item", "function_signature_item"}),
    anonymous=frozenset({"closure_expression"}),
    types={
        "struct_item": "class", "union_item": "class", "enum_item": "enum", "trait_item": "interface",
        "type_item": "type", "mod_item": "namespace", "impl_item": "impl",
    },
    values={"const_item": "constant", "static_item": "constant", "field_declaration": "field",
            "macro_definition": "macro"},
    implements_field="type",
    comment_kinds=frozenset({"line_comment", "block_comment"}),
    string_kinds=frozenset({"string_literal", "raw_string_literal"}),
    imports=ImportSpec(
        style="rust",
        statement_kinds=frozenset({"use_declaration", "extern_crate_declaration"}),
        module_field="argument",
        name_field="name",
        string_kinds=frozenset({"string_literal"}),
        call_kinds=frozenset({"call_expression"}),
    ),
)

JAVA = LanguageProfile(
    name="java",
    grammar="java",
    extensions=frozenset({".java"}),
    function_like=frozenset({"method_declaration", "constructor_declaration"}),
    anonymous=frozenset({"lambda_expression"}),
    types={
        "class_declaration": "class", "record_declaration": "class", "interface_declaration": "interface",
        "annotation_type_declaration": "interface", "enum_declaration": "enum",
    },
    values={"field_declaration": "field"},
    comment_kinds=frozenset({"line_comment", "block_comment"}),
    string_kinds=frozenset({"string_literal"}),
    imports=ImportSpec(
        style="java",
        statement_kinds=frozenset({"import_declaration"}),
        module_field="",
        name_field="name",
        string_kinds=frozenset({"string_literal"}),
        call_kinds=frozenset({"method_invocation"}),
    ),
)

_INCLUDES = ImportSpec(
    style="include",
    statement_kinds=frozenset({"preproc_include"}),
    module_field="path",
    string_kinds=frozenset({"string_literal", "system_lib_string"}),
)

CPP = LanguageProfile(
    name="cpp",
    grammar="cpp",
    extensions=frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h", ".ipp", ".inl"}),
    function_like=frozenset({"function_definition"}),
    anonymous=frozenset({"lambda_expression"}),
    types={
        "class_specifier": "class", "struct_specifier": "class", "union_specifier": "class",
        "enum_specifier": "enum", "namespace_definition": "namespace",
        "type_definition": "type", "alias_declaration": "type", "concept_definition": "interface",
    },
    values={"declaration": "variable", "field_declaration": "field",
            "preproc_def": "macro", "preproc_function_def": "macro"},
    wrappers={"template_declaration": Wrapper("template_declaration")},
    declarator_chain=True,
    constant_tokens=frozenset({"const", "constexpr"}),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string_literal", "raw_string_literal"}),
    imports=_INCLUDES,
)

C = LanguageProfile(
    name="c",
    grammar="c",
    extensions=frozenset({".c"}),
    function_like=frozenset({"function_definition"}),
    types={"struct_specifier": "class", "union_specifier": "class", "enum_specifier": "enum",
           "type_definition": "type"},
    values={"declaration": "variable", "field_declaration": "field",
            "preproc_def": "macro", "preproc_function_def": "macro"},
    declarator_chain=True,
    constant_tokens=frozenset({"const"}),
    comment_kinds=frozenset({"comment"}),
    string_kinds=frozenset({"string_literal"}),
    imports=_INCLUDES,
)

PROFILES: dict[str, LanguageProfile] = {
    profile.name: profile for profile in (PYTHON, TYPESCRIPT, TSX, JAVASCRIPT, GO, RUST, JAVA, CPP, C)
}

_BY_EXTENSION: dict[str, LanguageProfile] = {
    extension: profile for profile in PROFILES.values() for extension in profile.extensions
}


def suffix_of(path: str) -> str:
    """The extension of a path's last part, as `PurePosixPath(path).suffix` gives it. Asked of every
    file of a view on every query, where building a path object for each was a third of the time."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    at = name.rfind(".")
    return name[at:] if 0 < at < len(name) - 1 else ""


def stem_of(path: str) -> str:
    """A path's last part without its extension, as `PurePosixPath(path).stem` gives it."""
    name = path.rstrip("/").rsplit("/", 1)[-1]
    at = name.rfind(".")
    return name[:at] if 0 < at < len(name) - 1 else name


def profile_for_path(path: str) -> LanguageProfile | None:
    """The profile for a file, or None if it is not source code in a known language.
    A `.h` file is read as C++, which accepts nearly all C headers."""
    return _BY_EXTENSION.get(suffix_of(path).lower())
