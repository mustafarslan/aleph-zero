"""The code graph: a call resolved to definitions, with how it was found and how sure it is."""
from aleph_zero import lang
from aleph_zero.graph import FEATURES, FileFacts, Graph
from aleph_zero.lang import parse
from aleph_zero.lang.bindings import bases, bindings
from aleph_zero.lang.calls import calls
from aleph_zero.lang.definitions import definitions

MANIFESTS = ("go.mod", "Cargo.toml", "package.json", "tsconfig.json")


def build(files: dict[str, str], features=FEATURES):
    facts, sites, manifests = {}, {}, {}
    for path, source in files.items():
        data = source.encode()
        if path.rsplit("/", 1)[-1] in MANIFESTS:
            manifests[path] = data
            continue
        profile = lang.profile_for_path(path)
        tree = parse.tree(profile, data)
        found = definitions(profile, tree, data)
        facts[path] = FileFacts(profile.name, found, bindings(profile, tree, data), bases(profile, tree, data))
        sites[path] = calls(profile, tree, data, found)
    return Graph(facts, manifests, features), sites


def edge(graph, sites, path, name, nth=0):
    call = [call for call in sites[path] if call.name == name][nth]
    return graph.resolve(path, call)


def summary(found):
    return None if found is None else (found.level, found.provenance, [(t.path, t.qualified) for t in found.targets])


def answers(files, path, name, nth=0, features=FEATURES):
    graph, sites = build(files, features)
    return summary(edge(graph, sites, path, name, nth))


PY = {
    "pkg/__init__.py": "from .core import Engine\nfrom .util import slugify\n",
    "pkg/core.py": ("import os\nfrom .util import slugify\nfrom . import util\nfrom .base import Base\n\n"
                    "class Engine(Base):\n    def __init__(self):\n        super().__init__()\n"
                    "    def run(self, filter, other):\n        self.prepare()\n        slugify('x')\n        util.clean('y')\n"
                    "        local()\n        filter(1)\n        self.inherited()\n        self.missing()\n        os.path.join('a')\n"
                    "        return other.save()\n    def prepare(self):\n        pass\n\n"
                    "def local():\n    return local()\n"),
    "pkg/base.py": "class Base:\n    def __init__(self):\n        pass\n    def inherited(self):\n        pass\n",
    "pkg/util.py": "def slugify(text):\n    return text\n\ndef clean(text):\n    return text\n\ndef _private():\n    pass\n\ndef filter(x):\n    pass\n\ndef join(x):\n    pass\n",
    "pkg/store.py": "class Store:\n    def save(self):\n        pass\n    def missing(self):\n        pass\n\nclass Cache:\n    def save(self):\n        pass\n",
    "app.py": "from pkg import Engine, slugify\nfrom requests import get\n\ndef main():\n    Engine()\n    slugify('a')\n    _private()\n    nowhere()\n    get('u')\n",
    "other/get.py": "def get(url):\n    pass\n",
}


def test_a_call_to_something_in_the_same_file_is_certain():
    assert answers(PY, "pkg/core.py", "local") == ("L0", "syntactic", [("pkg/core.py", "local")])
    assert answers(PY, "pkg/core.py", "local", nth=1) == ("L0", "syntactic", [("pkg/core.py", "local")])     # recursion
    assert answers(PY, "pkg/core.py", "prepare") == ("L0", "syntactic", [("pkg/core.py", "Engine.prepare")])


def test_an_imported_name_is_followed_to_the_file_that_defines_it():
    assert answers(PY, "pkg/core.py", "slugify") == ("L1", "import-resolved", [("pkg/util.py", "slugify")])
    assert answers(PY, "pkg/core.py", "clean") == ("L1", "import-resolved", [("pkg/util.py", "clean")])


def test_a_name_republished_by_a_package_is_followed_through_it():
    assert answers(PY, "app.py", "Engine") == ("L1", "import-resolved", [("pkg/core.py", "Engine")])
    assert answers(PY, "app.py", "slugify") == ("L1", "import-resolved", [("pkg/util.py", "slugify")])
    assert answers(PY, "app.py", "slugify", features=FEATURES - {"reexports"}) != answers(PY, "app.py", "slugify")


def test_a_method_of_a_base_class_is_found_through_the_base():
    assert answers(PY, "pkg/core.py", "inherited") == ("L1", "import-resolved", [("pkg/base.py", "Base.inherited")])
    assert answers(PY, "pkg/core.py", "__init__") == ("L1", "import-resolved", [("pkg/base.py", "Base.__init__")])  # super(), not itself


def test_what_only_looks_like_a_project_function_gets_no_answer():
    assert answers(PY, "pkg/core.py", "filter") is None         # a parameter of that name, not util.filter
    assert answers(PY, "pkg/core.py", "join") is None           # os.path.join: the import leads outside these files
    assert answers(PY, "app.py", "get") is None                 # from requests import get, though other/get.py has one
    assert answers(PY, "pkg/core.py", "missing") is None        # on self; its class and base lack it, Store.missing is another's
    assert answers(PY, "app.py", "_private") is None            # private to another file
    assert answers(PY, "app.py", "nowhere") is None             # defined nowhere


def test_a_method_call_on_an_unknown_object_is_every_method_of_that_name_and_says_so():
    graph, sites = build(PY)
    found = edge(graph, sites, "pkg/core.py", "save")
    assert summary(found) == ("L1", "name", [("pkg/store.py", "Store.save"), ("pkg/store.py", "Cache.save")])
    assert found.confidence == 0.5                              # two candidates: not a confident answer


def test_a_callee_whose_file_is_not_among_the_files_gets_no_answer():
    without = {path: source for path, source in PY.items() if path != "pkg/util.py"}
    assert answers(without, "pkg/core.py", "slugify") is None
    assert answers(without, "pkg/core.py", "clean") is None


def test_a_name_with_too_many_definitions_is_not_answered():
    files = {f"m{n}.py": "class C:\n    def save(self):\n        pass\n" for n in range(25)}
    files["use.py"] = "def f(x):\n    x.save()\n"
    assert answers(files, "use.py", "save") is None


TS = {
    "src/index.ts": "export { makeUser } from './user';\nexport * from './lib/helper';\n",
    "src/user.ts": "export function makeUser() { return 1; }\nexport class User extends Base { greet() { this.name(); super.greet(); } name() {} }\nclass Base { greet() {} }\n",
    "src/app.ts": ("import { makeUser, helper as h } from './index';\nimport * as lib from './lib/helper';\nimport Tool from './tool';\n"
                   "import { User } from './user';\nimport { format } from 'date-fns';\nimport { shared } from '@acme/common';\n"
                   "export function run() { makeUser(); h(); lib.helper(); new Tool(); User.create(); format(); shared(); }\n"),
    "src/lib/helper.ts": "export function helper() { return 2; }\nexport function format() {}\n",
    "src/tool.ts": "export default class Tool {}\n",
    "packages/common/package.json": '{"name": "@acme/common", "main": "index.js"}',
    "packages/common/index.ts": "export * from './shared';\n",
    "packages/common/shared.ts": "export function shared() {}\n",
}


def test_typescript_follows_relative_imports_barrels_aliases_and_default_exports():
    assert answers(TS, "src/app.ts", "makeUser") == ("L1", "import-resolved", [("src/user.ts", "makeUser")])
    assert answers(TS, "src/app.ts", "h") == ("L1", "import-resolved", [("src/lib/helper.ts", "helper")])
    assert answers(TS, "src/app.ts", "helper") == ("L1", "import-resolved", [("src/lib/helper.ts", "helper")])
    assert answers(TS, "src/app.ts", "Tool") == ("L1", "import-resolved", [("src/tool.ts", "Tool")])
    assert answers(TS, "src/app.ts", "format") is None          # date-fns is not in these files; lib/helper's format is another
    assert answers(TS, "src/user.ts", "name") == ("L0", "syntactic", [("src/user.ts", "User.name")])
    assert answers(TS, "src/user.ts", "greet") == ("L0", "syntactic", [("src/user.ts", "Base.greet")])


def test_a_package_of_the_same_repository_is_found_by_its_name():
    assert answers(TS, "src/app.ts", "shared") == ("L1", "import-resolved", [("packages/common/shared.ts", "shared")])
    assert answers(TS, "src/app.ts", "shared", features=FEATURES - {"packages"}) is None


JS = {
    "lib/linter.js": "const utils = require('./utils');\nconst { a, b: c } = require('./ab');\nconst Tracker = require('./tracker');\nconst debug = require('debug');\n"
                     "function lint(node) {\n  utils.walk(node);\n  a();\n  c();\n  new Tracker();\n  debug('x');\n  function report() {}\n  report();\n}\nmodule.exports = { lint };\n",
    "lib/utils.js": "function walk(node) {}\nfunction debug() {}\nmodule.exports = { walk, renamed: walk };\n",
    "lib/ab.js": "function a() {}\nfunction b() {}\nmodule.exports = { a, b };\n",
    "lib/tracker.js": "class Tracker {}\nmodule.exports = Tracker;\n",
}


def test_commonjs_requires_are_followed():
    assert answers(JS, "lib/linter.js", "walk") == ("L1", "import-resolved", [("lib/utils.js", "walk")])
    assert answers(JS, "lib/linter.js", "a") == ("L1", "import-resolved", [("lib/ab.js", "a")])
    assert answers(JS, "lib/linter.js", "c") == ("L1", "import-resolved", [("lib/ab.js", "b")])
    assert answers(JS, "lib/linter.js", "Tracker") == ("L1", "import-resolved", [("lib/tracker.js", "Tracker")])
    assert answers(JS, "lib/linter.js", "debug") is None        # the package `debug`, not utils.js's function
    assert answers(JS, "lib/linter.js", "report") is None       # a function declared inside the caller: nothing recorded


GO = {
    "go.mod": "module example.com/kit\n\ngo 1.21\n",
    "metrics/generic/generic.go": "package generic\n\ntype Gauge struct{}\n\nfunc NewGauge() *Gauge { return nil }\nfunc (g *Gauge) Set(v float64) { g.add(v) }\nfunc (g *Gauge) add(v float64) {}\nfunc helper() {}\n",
    "metrics/generic/more.go": "package generic\n\nfunc (g *Gauge) With() { g.Set(1); helper() }\n",
    "metrics/statsd/statsd.go": ("package statsd\n\nimport (\n\t\"fmt\"\n\t\"example.com/kit/metrics/generic\"\n\tlog \"github.com/go-kit/log\"\n)\n\n"
                                 "type Gauge struct{ generic.Gauge }\n\nfunc (g *Gauge) add(v float64) {}\nfunc Info() {}\n"
                                 "func New() { generic.NewGauge(); log.Info(); fmt.Println(); helper() }\nfunc (g *Gauge) Push() { g.add(1); g.Set(2) }\n"),
}


def test_go_packages_receivers_and_embedded_types():
    assert answers(GO, "metrics/statsd/statsd.go", "NewGauge") == ("L1", "import-resolved", [("metrics/generic/generic.go", "NewGauge")])
    assert answers(GO, "metrics/statsd/statsd.go", "Info") is None      # go-kit/log is outside; statsd.Info is another function
    assert answers(GO, "metrics/statsd/statsd.go", "helper") is None    # generic's helper is private to its package
    assert answers(GO, "metrics/statsd/statsd.go", "add") == ("L0", "syntactic", [("metrics/statsd/statsd.go", "Gauge.add")])
    assert answers(GO, "metrics/statsd/statsd.go", "Set") == ("L1", "import-resolved", [("metrics/generic/generic.go", "Gauge.Set")])
    assert answers(GO, "metrics/generic/more.go", "Set") == ("L1", "import-resolved", [("metrics/generic/generic.go", "Gauge.Set")])
    assert answers(GO, "metrics/generic/more.go", "helper") == ("L1", "import-resolved", [("metrics/generic/generic.go", "helper")])


RUST = {
    "Cargo.toml": '[package]\nname = "app"\n\n[[bin]]\nname = "app"\npath = "core/main.rs"\n',
    "core/main.rs": "mod flags;\nmod messages;\nuse matcher::find;\n\nfn main() {\n    flags::parse();\n    messages::errored();\n    crate::flags::config::args();\n    find();\n    Vec::new();\n}\n",
    "core/messages.rs": "pub fn errored() {}\n",
    "core/flags/mod.rs": "pub mod config;\npub use self::config::args as arguments;\n\npub fn parse() {\n    config::args();\n    arguments();\n}\n",
    "core/flags/config.rs": ("pub struct Config;\nimpl Config {\n    pub fn new() -> Config { Config }\n    fn load(&self) { self.read(); Self::new(); super::parse(); }\n    fn read(&self) {}\n}\n"
                             "pub fn args() {}\nfn check() {}\nmod tests {\n    use super::*;\n    fn check() {}\n    fn t() { check(); super::check(); args(); Config::new(); }\n}\n"),
    "crates/matcher/Cargo.toml": '[package]\nname = "matcher"\n',
    "crates/matcher/src/lib.rs": "mod imp;\npub use crate::imp::find;\n",
    "crates/matcher/src/imp.rs": "pub fn find() {}\npub fn errored() {}\n",
}


def test_rust_paths_modules_and_crates():
    assert answers(RUST, "core/main.rs", "parse") == ("L1", "import-resolved", [("core/flags/mod.rs", "parse")])
    assert answers(RUST, "core/main.rs", "errored") == ("L1", "import-resolved", [("core/messages.rs", "errored")])
    assert answers(RUST, "core/main.rs", "args") == ("L1", "import-resolved", [("core/flags/config.rs", "args")])
    assert answers(RUST, "core/main.rs", "find") == ("L1", "import-resolved", [("crates/matcher/src/imp.rs", "find")])
    assert answers(RUST, "core/main.rs", "new") is None                 # Vec::new: not these files' `new`
    assert answers(RUST, "core/flags/mod.rs", "args") == ("L1", "import-resolved", [("core/flags/config.rs", "args")])
    assert answers(RUST, "core/flags/mod.rs", "arguments") == ("L1", "import-resolved", [("core/flags/config.rs", "args")])
    assert answers(RUST, "core/flags/config.rs", "read") == ("L0", "syntactic", [("core/flags/config.rs", "Config.read")])
    assert answers(RUST, "core/flags/config.rs", "new") == ("L0", "syntactic", [("core/flags/config.rs", "Config.new")])
    assert answers(RUST, "core/flags/config.rs", "parse") == ("L1", "import-resolved", [("core/flags/mod.rs", "parse")])


def test_rust_inline_modules_see_their_own_names_first():
    assert answers(RUST, "core/flags/config.rs", "check") == ("L0", "syntactic", [("core/flags/config.rs", "tests.check")])
    assert answers(RUST, "core/flags/config.rs", "check", nth=1) == ("L0", "syntactic", [("core/flags/config.rs", "check")])
    assert answers(RUST, "core/flags/config.rs", "args") == ("L0", "syntactic", [("core/flags/config.rs", "args")])


JAVA = {
    "src/main/java/org/x/owner/Owner.java": "package org.x.owner;\nimport org.x.model.Person;\npublic class Owner extends Person {\n  void add() { getName(); helper(); Util.help(); new Pet(); }\n  void helper() {}\n  void helper(int n) {}\n}\n",
    "src/main/java/org/x/owner/Pet.java": "package org.x.owner;\npublic class Pet {}\n",
    "src/main/java/org/x/owner/Util.java": "package org.x.owner;\nclass Util {\n  static void help() {}\n}\n",
    "src/main/java/org/x/model/Person.java": "package org.x.model;\npublic class Person {\n  public String getName() { return null; }\n}\n",
}


def test_java_packages_inheritance_and_overloads():
    assert answers(JAVA, "src/main/java/org/x/owner/Owner.java", "getName") == (
        "L1", "import-resolved", [("src/main/java/org/x/model/Person.java", "Person.getName")])
    assert answers(JAVA, "src/main/java/org/x/owner/Owner.java", "help") == (
        "L1", "import-resolved", [("src/main/java/org/x/owner/Util.java", "Util.help")])
    assert answers(JAVA, "src/main/java/org/x/owner/Owner.java", "Pet") == (
        "L1", "import-resolved", [("src/main/java/org/x/owner/Pet.java", "Pet")])
    overloads = answers(JAVA, "src/main/java/org/x/owner/Owner.java", "helper")
    assert overloads == ("L0", "syntactic", [("src/main/java/org/x/owner/Owner.java", "Owner.helper")] * 2)   # two: not confident


def test_imports_that_lead_in_circles_end():
    files = {"a.py": "from b import thing\n\ndef f():\n    thing()\n", "b.py": "from a import thing\n"}
    assert answers(files, "a.py", "thing") is None
    files = {"src/lib.rs": "mod a;\nmod b;\n", "src/a.rs": "pub use crate::b::*;\nfn f() { g(); }\n", "src/b.rs": "pub use crate::a::*;\n"}
    assert answers(files, "src/a.rs", "g") is None
