from pathlib import Path

from aleph_zero import lang
from aleph_zero.lang import parse
from aleph_zero.lang.imports import extract_imports

SAMPLES = Path(__file__).parent / "samples"


def imports(name, source=None, file=None):
    profile = lang.PROFILES[name]
    data = (SAMPLES / file).read_bytes() if file else source.encode()
    return extract_imports(parse.tree(profile, data).root_node, profile)


def brief(items):
    return [(item.specifier, item.names, item.level, item.kind) for item in items]


def test_python_imports():
    assert brief(imports("python", file="sample.py")) == [
        ("os", (), 0, "value"),
        ("typing", ("Optional",), 0, "value"),
        ("session", ("Session", "refresh"), 1, "value"),
    ]


def test_python_type_checking_and_deferred_imports():
    source = (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .models import User\n\n"
        "def load():\n    import json\n    return json\n\nfrom ..pkg import *\n"
    )
    items = {item.specifier: item for item in imports("python", source)}
    assert items["models"].kind == "type_only" and items["models"].level == 1
    assert items["json"].deferred and not items["typing"].deferred
    assert items["pkg"].level == 2 and items["pkg"].is_relative


def test_typescript_imports():
    items = imports("typescript", file="sample.ts")
    assert [(item.specifier, item.kind) for item in items] == [
        ("./session", "value"), ("../config", "type_only"), ("node:path", "value"),
    ]
    assert items[0].names == ("Session", "refresh") and items[0].is_relative
    assert items[1].names == ("Config",) and items[2].names == ("*",)
    more = imports("typescript", 'import React, { useState } from "react";\nexport { a as b } from "./a";\nexport * from "./all";\nimport "./side";\n')
    assert [(item.specifier, item.names, item.kind, item.reexport) for item in more] == [
        ("react", ("default", "useState"), "value", False), ("./a", ("a",), "value", True),
        ("./all", ("*",), "value", True), ("./side", (), "side_effect", False),
    ]


def test_javascript_require_and_reexport():
    items = {item.specifier: item for item in imports("javascript", file="sample.js")}
    assert set(items) == {"node:path", "./other"}
    assert items["./other"].reexport and not items["node:path"].reexport


def test_go_imports():
    assert [item.specifier for item in imports("go", file="sample.go")] == ["fmt", "strings"]


def test_rust_imports():
    specifiers = [item.specifier for item in imports("rust", file="sample.rs")]
    assert specifiers == ["std.collections.HashMap", "crate.session.Session", "crate.session.refresh"]


def test_java_imports():
    items = imports("java", file="Sample.java")
    assert [item.specifier for item in items] == ["java.util.Map", "java.util.Objects", "com.example.session"]
    assert items[1].names == ("requireNonNull",)


def test_c_and_cpp_includes():
    assert brief(imports("cpp", file="sample.cpp")) == [
        ("string", (), 0, "system"), ("session.h", (), 0, "value"),
    ]
    assert brief(imports("c", file="sample.c")) == [
        ("stdio.h", (), 0, "system"), ("util.h", (), 0, "value"),
    ]
    guarded = imports("cpp", "#ifdef _WIN32\n#include <windows.h>\n#else\n#include \"posix/io.h\"\n#endif\n")
    assert [item.specifier for item in guarded] == ["windows.h", "posix/io.h"]
