"""Call sites read from source: the callee's name as written, what it is called on, where, and inside what."""
import pytest

from aleph_zero import lang
from aleph_zero.lang import parse
from aleph_zero.lang.calls import calls
from aleph_zero.lang.definitions import definitions


def found(name: str, source: str):
    profile = lang.PROFILES[name]
    data = source.encode()
    tree = parse.tree(profile, data)
    defs = definitions(profile, tree, data)
    return [(call.name, call.receiver, call.line, defs[call.caller].qualified if call.caller is not None else None)
            for call in calls(profile, tree, data, defs)]


def test_python_calls():
    assert found("python", (
        "import os\n"
        "def outer(x):\n"
        "    helper(x)\n"
        "    self.store.save(x, key=make_key())\n"
        "    return os.path.join('a', x)\n"
        "class Box:\n"
        "    def put(self):\n"
        "        Box()\n"
        "top_level()\n"
    )) == [
        ("helper", "", 3, "outer"),
        ("save", "self.store", 4, "outer"),
        ("make_key", "", 4, "outer"),
        ("join", "os.path", 5, "outer"),
        ("Box", "", 8, "Box.put"),
        ("top_level", "", 9, None),
    ]


def test_a_call_names_its_position_at_the_callee_not_at_the_statement():
    profile = lang.PROFILES["python"]
    data = b"def f():\n    return self.store.save(1)\n"
    tree = parse.tree(profile, data)
    call = calls(profile, tree, data, definitions(profile, tree, data))[0]
    assert (call.line, call.column) == (2, 22)          # where `save` is written: what an index of occurrences is keyed by


@pytest.mark.parametrize("name, source, expected", [
    ("typescript", "function run(a: Api) {\n  setup();\n  a.client.fetch(url);\n  new Widget(1);\n}\n",
     [("setup", "", 2, "run"), ("fetch", "a.client", 3, "run"), ("Widget", "", 4, "run")]),
    ("javascript", "const go = () => {\n  load();\n  this.items.push(1);\n};\n",
     [("load", "", 2, "go"), ("push", "this.items", 3, "go")]),
    ("go", "package main\nfunc Run(s *Store) {\n\tinit()\n\ts.Save(1)\n\tfmt.Println(\"x\")\n}\n",
     [("init", "", 3, "Run"), ("Save", "s", 4, "Run"), ("Println", "fmt", 5, "Run")]),
    ("rust", "fn run(s: &Store) {\n    setup();\n    s.save(1);\n    Store::new();\n    crate::util::log(1);\n}\n",
     [("setup", "", 2, "run"), ("save", "s", 3, "run"), ("new", "Store", 4, "run"), ("log", "crate::util", 5, "run")]),
    ("java", "class A {\n  void run(Store s) {\n    setup();\n    s.save(1);\n    new Widget(2);\n  }\n}\n",
     [("setup", "", 3, "A.run"), ("save", "s", 4, "A.run"), ("Widget", "", 5, "A.run")]),
    ("cpp", "void run(Store* s) {\n  setup();\n  s->save(1);\n  ns::helper(2);\n  obj.method();\n}\n",
     [("setup", "", 2, "run"), ("save", "s", 3, "run"), ("helper", "ns", 4, "run"), ("method", "obj", 5, "run")]),
    ("c", "int run(struct store *s) {\n  setup();\n  s->ops->save(s);\n  return 0;\n}\n",
     [("setup", "", 2, "run"), ("save", "s->ops", 3, "run")]),
])
def test_calls_in_the_other_languages(name, source, expected):
    assert found(name, source) == expected


def test_calls_that_have_no_name_are_left_out_and_nested_calls_are_all_found():
    assert found("python", "def f():\n    get()(1)\n    (lambda: 1)()\n    a(b(c()))\n") == [
        ("get", "", 2, "f"), ("a", "", 4, "f"), ("b", "", 4, "f"), ("c", "", 4, "f")]


def test_a_call_inside_a_nested_function_belongs_to_the_definition_that_encloses_it():
    """Functions defined inside functions are not definitions of their own in Aleph (they
    cannot be called from elsewhere), so what they call is their enclosing function's doing."""
    assert found("python", "def outer():\n    def inner():\n        work()\n    inner()\n") == [
        ("work", "", 3, "outer"), ("inner", "", 4, "outer")]
