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


def shaped(name: str, source: str):
    """Each call with how it is qualified and what its first name is bound to in the file."""
    profile = lang.PROFILES[name]
    data = source.encode()
    tree = parse.tree(profile, data)
    return [(call.receiver + "|" + call.name, call.via, call.head)
            for call in calls(profile, tree, data, definitions(profile, tree, data))]


def test_python_says_what_the_first_name_of_a_call_is_bound_to():
    assert shaped("python", (
        "import os\n"
        "class Box:\n"
        "    def put(self, item, make):\n"
        "        self.check(item)\n"              # on the method's own receiver
        "        item.save()\n"                   # on a parameter
        "        make()\n"                        # a parameter called
        "        def inner():\n"
        "            self.again()\n"              # the receiver, seen from a nested function
        "        inner()\n"                       # a nested function: local
        "        handle = open('x')\n"
        "        handle.close()\n"                # a local variable
        "        os.getcwd()\n"                   # bound outside any function
        "        super().put(item, make)\n"
        "        helper()(1).run()\n"             # called on a value with no name
        "    @staticmethod\n"
        "    def build(first):\n"
        "        first.go()\n"                    # a static method has no receiver
    )) == [
        ("self|check", "member", "self"), ("item|save", "member", "local"), ("|make", "", "local"),
        ("self|again", "member", "self"), ("|inner", "", "local"), ("|open", "", ""), ("handle|close", "member", "local"),
        ("os|getcwd", "member", ""), ("|super", "", ""), ("super()|put", "member", "super"),
        ("|helper", "", ""), ("helper()(1)|run", "member", "value"), ("first|go", "member", "local"),
    ]


@pytest.mark.parametrize("name,source,expected", [
    ("go", "package a\nfunc (g *Gauge) Add(x int) {\n\tg.add(x)\n\tg.inner.Set(x)\n\tv := generic.NewGauge()\n\tv.Set(1)\n\tfor _, item := range g.items {\n\t\titem.Run()\n\t}\n\thelper(x)\n}\n",
     [("g|add", "member", "self"), ("g.inner|Set", "member", "self"), ("generic|NewGauge", "member", ""), ("v|Set", "member", "local"),
      ("item|Run", "member", "local"), ("|helper", "", "")]),
    ("rust", "impl S {\n    fn go(&self, other: &T) {\n        self.step();\n        Self::make();\n        other.run();\n        config::args();\n"
             "        crate::flags::parse();\n        let filter = |x| x;\n        filter(1);\n        super::check();\n        helper();\n    }\n}\n",
     [("self|step", "member", "self"), ("Self|make", "path", "self"), ("other|run", "member", "local"), ("config|args", "path", ""),
      ("crate::flags|parse", "path", ""), ("|filter", "", "local"), ("super|check", "path", ""), ("|helper", "", "")]),
    ("typescript", "class A extends B {\n  constructor(private readonly svc: Service) { super(); }\n  run(req: Req) {\n    this.svc.handle(req);\n    this.own();\n"
                   "    super.run(req);\n    req.end();\n    const made = new Thing();\n    made.go();\n    [1].forEach(n => n.toFixed());\n    Util.help();\n  }\n}\n",
     [("this.svc|handle", "member", "self"), ("this|own", "member", "self"), ("super|run", "member", "super"),
      ("req|end", "member", "local"), ("|Thing", "", ""), ("made|go", "member", "local"), ("[1]|forEach", "member", "value"),
      ("n|toFixed", "member", "local"), ("Util|help", "member", "")]),
    ("javascript", "const utils = require('./utils');\nfunction f(node) {\n  const { a } = require('./a');\n  a();\n  utils.b(node);\n  node.c();\n  try { d(); } catch (err) { err.e(); }\n}\n",
     [("|require", "", ""), ("|require", "", ""), ("|a", "", ""), ("utils|b", "member", ""), ("node|c", "member", "local"), ("|d", "", ""),
      ("err|e", "member", "local")]),
    ("java", "class A extends B {\n  void m(Owner owner) {\n    owner.getName();\n    this.save(owner);\n    helper();\n    super.m(owner);\n    Pet pet = new Pet();\n    pet.setName(\"x\");\n    Util.help();\n    owners.findAll();\n  }\n}\n",
     [("owner|getName", "member", "local"), ("this|save", "member", "self"), ("|helper", "", ""), ("super|m", "member", "super"), ("|Pet", "", ""),
      ("pet|setName", "member", "local"), ("Util|help", "member", ""), ("owners|findAll", "member", "")]),
])
def test_the_other_languages_say_it_too(name, source, expected):
    assert shaped(name, source) == expected
