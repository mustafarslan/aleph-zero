"""What a file's imports let it write, and what its types are built on."""
import pytest

from aleph_zero import lang
from aleph_zero.lang import parse
from aleph_zero.lang.bindings import bases, bindings
from aleph_zero.lang.definitions import definitions


def bound(name: str, source: str):
    profile = lang.PROFILES[name]
    tree = parse.tree(profile, source.encode())
    return [(b.local, b.source, b.name) + ((b.level,) if b.level else ()) + ((b.kind,) if b.kind != "import" else ())
            for b in bindings(profile, tree, source.encode())]


def test_python_imports():
    assert bound("python", (
        "import os.path\nimport pkg.mod as m\nfrom .models import Request, Response as R\nfrom . import util\n"
        "from ..core import *\nfrom __future__ import annotations\n"
        "def f():\n    from late import thing\n"
    )) == [
        ("os", "os", ""), ("m", "pkg.mod", ""), ("Request", "models", "Request", 1), ("R", "models", "Response", 1),
        ("util", "", "util", 1), ("*", "core", "*", 2), ("thing", "late", "thing"),
    ]


def test_typescript_imports_and_what_a_file_republishes():
    assert bound("typescript", (
        "import Def, { makeUser, B as C } from './user';\nimport * as ns from '../lib/x';\nimport type { T } from './types';\n"
        "export * from './all';\nexport { q, r as s } from './q';\nexport * as tools from './tools';\n"
        "import legacy = require('./legacy');\nfunction a() {}\nexport { a as b };\nexport default a;\n"
    )) == [
        ("Def", "./user", "default"), ("makeUser", "./user", "makeUser"), ("C", "./user", "B"), ("ns", "../lib/x", ""),
        ("T", "./types", "T"), ("*", "./all", "*"), ("q", "./q", "q"), ("s", "./q", "r"), ("tools", "./tools", ""),
        ("legacy", "./legacy", ""), ("b", "", "a", "export"), ("default", "", "a", "export"),
    ]


def test_commonjs_requires_and_exports():
    assert bound("javascript", (
        "const utils = require('./utils');\nconst { a, b: c } = require('./ab');\nconst one = require('./many').one;\n"
        "function f() { const late = require('./late'); }\nclass K {}\n"
        "module.exports = { a, renamed: f, K };\nexports.other = f;\n"
    )) == [
        ("utils", "./utils", ""), ("a", "./ab", "a"), ("c", "./ab", "b"), ("one", "./many", "one"), ("late", "./late", ""),
        ("renamed", "", "f", "export"), ("other", "", "f", "export"),
    ]
    assert bound("javascript", "class Linter {}\nmodule.exports = Linter;\n") == [("default", "", "Linter", "export")]
    assert bound("javascript", "module.exports = class Rule {};\n") == [("default", "", "Rule", "export")]
    assert bound("javascript", "module.exports = require('./other');\n") == [("*", "./other", "*")]
    assert bound("javascript", "export default function main() {}\n") == [("default", "", "main", "export")]


def test_go_imports_name_the_package_and_the_file_says_its_own():
    assert bound("go", (
        'package level_test\nimport (\n\t"fmt"\n\t"github.com/go-kit/kit/metrics/generic"\n\tkitlog "github.com/go-kit/log"\n'
        '\t. "x/y"\n\t_ "z"\n\t"github.com/casbin/casbin/v2"\n\t"gopkg.in/yaml.v2"\n)\n'
    )) == [
        ("level_test", "", "", "package"), ("fmt", "fmt", ""), ("generic", "github.com/go-kit/kit/metrics/generic", ""),
        ("kitlog", "github.com/go-kit/log", ""), ("*", "x/y", "*"), ("casbin", "github.com/casbin/casbin/v2", ""),
        ("yaml", "gopkg.in/yaml.v2", ""),
    ]


def test_rust_uses_and_module_declarations():
    assert bound("rust", (
        "use crate::searcher::{Searcher, glue::MultiLine, self};\nuse super::util;\nuse grep_matcher::Matcher as M;\n"
        "pub use self::inner::Thing;\nuse crate::z::*;\nmod inner;\nextern crate log;\n"
        "mod tests {\n    use super::*;\n    use crate::a::b;\n}\n"
    )) == [
        ("Searcher", "crate::searcher", "Searcher"), ("MultiLine", "crate::searcher::glue", "MultiLine"),
        ("searcher", "crate", "searcher"), ("util", "super", "util"), ("M", "grep_matcher", "Matcher"),
        ("Thing", "self::inner", "Thing"), ("*", "crate::z", "*"), ("inner", "self", "inner", "module"), ("log", "", "log"),
        ("*", "super", "*", 1), ("b", "crate::a", "b", 1),
    ]


def test_java_imports_and_package():
    assert bound("java", (
        "package org.x.owner;\nimport org.x.model.Person;\nimport java.util.*;\nimport static org.x.Util.help;\nimport static org.x.Util.*;\nclass A {}\n"
    )) == [
        ("org.x.owner", "", "", "package"), ("Person", "org.x.model", "Person"), ("*", "java.util", "*"), ("help", "org.x.Util", "help"),
        ("*", "org.x.Util", "*"),
    ]


@pytest.mark.parametrize("name,source,expected", [
    ("python", "class A(Base, mod.Mixin, metaclass=M):\n    class Inner(A):\n        pass\nclass Plain:\n    pass\n",
     [("A", "Base"), ("A", "mod.Mixin"), ("A.Inner", "A")]),
    ("typescript", "class A extends B<T> implements I, J {}\ninterface K extends I {}\nabstract class V extends ns.W {}\n",
     [("A", "B"), ("A", "I"), ("A", "J"), ("K", "I"), ("V", "ns.W")]),
    ("javascript", "class A extends B {}\nclass C extends ns.D {}\n", [("A", "B"), ("C", "ns.D")]),
    ("java", "class A extends B implements I, J<T> {}\ninterface K extends I {}\n", [("A", "B"), ("A", "I"), ("A", "J"), ("K", "I")]),
    ("go", "package a\ntype Gauge struct {\n\t*generic.Gauge\n\tBase\n\tname string\n}\ntype RW interface {\n\tReader\n\tWrite()\n}\n",
     [("Gauge", "generic.Gauge"), ("Gauge", "Base"), ("RW", "Reader")]),
    ("rust", "struct S;\nimpl Matcher for S {}\nimpl<T> fmt::Display for W<T> {}\nimpl S {}\ntrait Sub: Super {}\n",
     [("S", "Matcher"), ("W", "fmt::Display"), ("Sub", "Super")]),
])
def test_what_a_type_is_built_on(name, source, expected):
    profile = lang.PROFILES[name]
    tree = parse.tree(profile, source.encode())
    assert bases(profile, tree, source.encode()) == expected
