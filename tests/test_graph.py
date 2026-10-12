"""The code graph: a call resolved to definitions, with how it was found and how sure it is."""
from aleph_zero import lang
from aleph_zero.graph import FEATURES, FileFacts, Graph
from aleph_zero.lang import parse
from aleph_zero.lang.bindings import bases, bindings, fields
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
        facts[path] = FileFacts(profile.name, found, bindings(profile, tree, data), bases(profile, tree, data),
                                fields(profile, tree, data))
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
    assert answers(JS, "lib/linter.js", "report") == ("L0", "syntactic", [("lib/linter.js", "lint.report")])   # declared inside the caller


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
    graph, sites = build(JAVA)
    chosen = edge(graph, sites, "src/main/java/org/x/owner/Owner.java", "helper")
    assert [(t.qualified, t.start_line) for t in chosen.targets] == [("Owner.helper", 5)]    # of two overloads, the one that takes no argument


def test_imports_that_lead_in_circles_end():
    files = {"a.py": "from b import thing\n\ndef f():\n    thing()\n", "b.py": "from a import thing\n"}
    assert answers(files, "a.py", "thing") is None
    files = {"src/lib.rs": "mod a;\nmod b;\n", "src/a.rs": "pub use crate::b::*;\nfn f() { g(); }\n", "src/b.rs": "pub use crate::a::*;\n"}
    assert answers(files, "src/a.rs", "g") is None


TYPED_PY = {
    "pkg/store.py": "class Store:\n    def save(self):\n        pass\n\nclass Cache(Store):\n    def load(self):\n        pass\n\nclass Other:\n    def save(self):\n        pass\n    def get(self):\n        pass\n    def append(self, x):\n        pass\n",
    "pkg/use.py": ("from .store import Store, Cache\nfrom requests import Session\n\n"
                   "def f(store: Store, session: Session, items: list, loose):\n    store.save()\n    session.get()\n    items.append(1)\n"
                   "    made = Cache()\n    made.save()\n    made.load()\n    loose.load()\n\n"
                   "class Service:\n    def __init__(self, store: Store):\n        self.store = store\n        self.cache = Cache()\n        self.session = Session()\n"
                   "    def run(self):\n        self.store.save()\n        self.cache.load()\n        self.session.get()\n        self.unknown.save()\n"),
}


def test_a_call_on_a_value_of_a_declared_type_reaches_that_type_and_no_other():
    path = "pkg/use.py"
    assert answers(TYPED_PY, path, "save") == ("L1", "import-resolved", [("pkg/store.py", "Store.save")])
    assert answers(TYPED_PY, path, "save", nth=1) == ("L1", "import-resolved", [("pkg/store.py", "Store.save")])   # Cache(), through its base
    assert answers(TYPED_PY, path, "load") == ("L1", "import-resolved", [("pkg/store.py", "Cache.load")])
    without = answers(TYPED_PY, path, "save", features=FEATURES - {"receivers"})
    assert without[1] == "name" and len(without[2]) == 2                # without the types: two candidates, by name


def test_a_declared_type_from_outside_these_files_gets_no_answer():
    assert answers(TYPED_PY, "pkg/use.py", "get") is None               # requests.Session, though Other.get shares the name
    assert answers(TYPED_PY, "pkg/use.py", "append") is None            # a list, though Other.append shares the name
    assert answers(TYPED_PY, "pkg/use.py", "get", nth=1) is None        # self.session = Session()


def test_a_value_of_no_declared_type_is_still_answered_by_name_and_says_so():
    assert answers(TYPED_PY, "pkg/use.py", "load", nth=1) == ("L1", "name", [("pkg/store.py", "Cache.load")])
    graph, sites = build(TYPED_PY)
    assert edge(graph, sites, "pkg/use.py", "load", 1).certain is False     # one candidate, found by name: not certain
    assert edge(graph, sites, "pkg/use.py", "load", 0).certain is True
    assert answers(TYPED_PY, "pkg/use.py", "save", nth=3)[1] == "name"      # self.unknown.save(): the field has no stated type


def test_a_call_through_a_field_follows_the_fields_type():
    path = "pkg/use.py"
    assert answers(TYPED_PY, path, "save", nth=2) == ("L1", "import-resolved", [("pkg/store.py", "Store.save")])
    assert answers(TYPED_PY, path, "load", nth=2) == ("L1", "import-resolved", [("pkg/store.py", "Cache.load")])
    go = {
        "go.mod": "module example.com/kit\n",
        "lv/lv.go": "package lv\n\ntype LabelValues []string\n\nfunc (lvs LabelValues) With(v ...string) LabelValues { return lvs }\n",
        "generic/generic.go": "package generic\n\ntype Gauge struct{}\n\nfunc (g *Gauge) With(v ...string) {}\nfunc (g *Gauge) Set(v float64) {}\n",
        "statsd/statsd.go": ("package statsd\n\nimport (\n\t\"example.com/kit/generic\"\n\t\"example.com/kit/lv\"\n\t\"sync\"\n)\n\n"
                             "type Counter struct {\n\tlvs lv.LabelValues\n\tg   *generic.Gauge\n\tmu  sync.Mutex\n}\n\n"
                             "func (c *Counter) With() { c.lvs.With(); c.g.With(); c.g.Set(1); c.mu.Lock() }\nfunc Lock() {}\ntype T struct{}\nfunc (t T) Lock() {}\n"),
    }
    assert answers(go, "statsd/statsd.go", "With") == ("L1", "import-resolved", [("lv/lv.go", "LabelValues.With")])
    assert answers(go, "statsd/statsd.go", "With", nth=1) == ("L1", "import-resolved", [("generic/generic.go", "Gauge.With")])
    assert answers(go, "statsd/statsd.go", "Lock") is None                  # sync.Mutex is outside; T.Lock is another type's
    ts = {
        "src/service.ts": "export class Service { handle() {} }\nexport class Other { handle() {} log() {} }\n",
        "src/a.ts": ("import { Service } from './service';\nimport { Logger } from '@nestjs/common';\n"
                     "export class A {\n  private readonly logger = new Logger('A');\n  constructor(private readonly svc: Service) {}\n"
                     "  run() { this.svc.handle(); this.logger.log(); }\n}\n"),
    }
    assert answers(ts, "src/a.ts", "handle") == ("L1", "import-resolved", [("src/service.ts", "Service.handle")])
    assert answers(ts, "src/a.ts", "log") is None
    java = {
        "a/Owners.java": "package a;\npublic class Owners {\n  void findAll() {}\n}\n",
        "a/Other.java": "package a;\npublic class Other {\n  void findAll() {}\n}\n",
        "a/Ctl.java": "package a;\npublic class Ctl {\n  private final Owners owners;\n  void list(Other other) { owners.findAll(); this.owners.findAll(); other.findAll(); }\n}\n",
    }
    assert answers(java, "a/Ctl.java", "findAll") == ("L1", "import-resolved", [("a/Owners.java", "Owners.findAll")])
    assert answers(java, "a/Ctl.java", "findAll", nth=1) == ("L1", "import-resolved", [("a/Owners.java", "Owners.findAll")])
    assert answers(java, "a/Ctl.java", "findAll", nth=2) == ("L1", "import-resolved", [("a/Other.java", "Other.findAll")])


def test_a_name_imported_twice_as_two_things_is_both_and_so_not_certain():
    files = {
        "src/lib.rs": "mod args;\nmod defs;\n",
        "src/args.rs": "pub struct Context;\nimpl Context { pub fn new() -> Context { Context } }\npub struct Field;\nimpl Field { pub fn new() -> Field { Field } }\n",
        "src/defs.rs": "fn a() {\n    use crate::args::Context as Separator;\n    Separator::new();\n}\nfn b() {\n    use crate::args::Field as Separator;\n    Separator::new();\n}\n",
    }
    graph, sites = build(files)
    found = edge(graph, sites, "src/defs.rs", "new")
    assert sorted(t.qualified for t in found.targets) == ["Context.new", "Field.new"] and not found.certain


def test_a_name_imported_one_way_or_another_is_either():
    files = {
        "fast.py": "def dumps(x):\n    pass\n", "slow.py": "def dumps(x):\n    pass\n",
        "use.py": "try:\n    from fast import dumps\nexcept ImportError:\n    from slow import dumps\n\ndef f():\n    dumps(1)\n",
    }
    assert answers(files, "use.py", "dumps") == ("L1", "import-resolved", [("fast.py", "dumps"), ("slow.py", "dumps")])


BUILT = {
    "pkg/store.py": "class Store:\n    def save(self):\n        pass\n\nclass Other:\n    def save(self):\n        pass\n\n"
                    "class Factory:\n    def build(self) -> Store:\n        return Store()\n\ndef make_store() -> Store:\n    return Store()\n\ndef untyped():\n    return Store()\n",
    "pkg/more.py": "from .store import make_store, untyped\n\ndef f(loose):\n    made = make_store()\n    made.save()\n    built = loose.build()\n    built.save()\n"
                   "    plain = untyped()\n    plain.save()\n",
}


def test_a_value_built_by_a_call_takes_the_type_the_callee_says_it_returns():
    assert answers(BUILT, "pkg/more.py", "save") == ("L1", "import-resolved", [("pkg/store.py", "Store.save")])
    # `loose.build()` is found by name only, so what it returns is not known; neither is the result of a function that states no type.
    assert answers(BUILT, "pkg/more.py", "save", nth=1) == ("L1", "name", [("pkg/store.py", "Store.save"), ("pkg/store.py", "Other.save")])
    assert answers(BUILT, "pkg/more.py", "save", nth=2) == ("L1", "name", [("pkg/store.py", "Store.save"), ("pkg/store.py", "Other.save")])
    go = {
        "go.mod": "module example.com/kit\n",
        "generic/generic.go": "package generic\n\ntype Gauge struct{}\n\nfunc NewGauge() *Gauge { return nil }\nfunc Pair() (*Gauge, error) { return nil, nil }\nfunc (g *Gauge) Set(v float64) {}\n",
        "other/other.go": "package other\n\ntype T struct{}\n\nfunc (t T) Set(v float64) {}\n",
        "use/use.go": "package use\n\nimport \"example.com/kit/generic\"\n\nfunc f() {\n\tz := generic.NewGauge()\n\tz.Set(1)\n}\n",
    }
    assert answers(go, "use/use.go", "Set") == ("L1", "import-resolved", [("generic/generic.go", "Gauge.Set")])
    rust = {
        "src/lib.rs": "mod b;\nuse crate::b::Builder;\nfn f() {\n    let made = Builder::new();\n    made.build();\n}\nstruct Other;\nimpl Other { fn build(&self) {} }\n",
        "src/b.rs": "pub struct Builder;\nimpl Builder {\n    pub fn new() -> Self { Builder }\n    pub fn build(&self) {}\n}\n",
    }
    assert answers(rust, "src/lib.rs", "build") == ("L1", "import-resolved", [("src/b.rs", "Builder.build")])


def test_an_absolute_python_import_is_never_looked_up_inside_the_importing_package():
    """`import json` inside the package `flask.json` is the standard library's, not the package itself:
    only a directory that is not a package can be where an absolute import starts."""
    files = {
        "src/flask/__init__.py": "",
        "src/flask/json/__init__.py": "import json as _json\n\ndef dumps(obj):\n    return _json.dumps(obj)\n",
        "src/flask/json/provider.py": "import json\nfrom flask.json import dumps as ours\n\ndef to_text(obj):\n    json.dumps(obj)\n    return ours(obj)\n",
    }
    assert answers(files, "src/flask/json/__init__.py", "dumps") is None
    assert answers(files, "src/flask/json/provider.py", "dumps") is None
    assert answers(files, "src/flask/json/provider.py", "ours") == ("L1", "import-resolved", [("src/flask/json/__init__.py", "dumps")])


# ---- where two rules of a language disagree, the resolver must not pick one and call it certain -------
# (the six shapes below were found by an adversarial review of the certain stamp)

def test_python_follows_the_method_resolution_order_not_the_first_base():
    diamond = {"m.py": "class Root:\n    def ping(self): pass\nclass A(Root): pass\nclass B(Root):\n    def ping(self): pass\n"
                       "class C(A, B):\n    def test(self):\n        self.ping()\n        super().ping()\n"}
    assert answers(diamond, "m.py", "ping") == ("L0", "syntactic", [("m.py", "B.ping")])
    assert answers(diamond, "m.py", "ping", nth=1) == ("L0", "syntactic", [("m.py", "B.ping")])
    deep_first = {"m.py": "class X:\n    def ping(self): pass\nclass A(X): pass\nclass B:\n    def ping(self): pass\n"
                          "class C(A, B):\n    def test(self):\n        self.ping()\n"}
    assert answers(deep_first, "m.py", "ping") == ("L0", "syntactic", [("m.py", "X.ping")])    # C, A, X, B


def test_go_takes_the_promoted_method_of_the_shallowest_embedding():
    files = {"go.mod": "module x\n", "p/p.go": "package p\ntype Deep struct{}\nfunc (Deep) Run() {}\ntype Container struct{ Deep }\n"
                                                "type Shallow struct{}\nfunc (Shallow) Run() {}\ntype S struct {\n\tContainer\n\tShallow\n}\n"
                                                "func (s S) Test() { s.Run() }\n"}
    assert answers(files, "p/p.go", "Run") == ("L0", "syntactic", [("p/p.go", "Shallow.Run")])


def test_java_overloads_are_told_apart_by_the_number_of_arguments_and_no_further():
    files = {
        "a/Base.java": "package a;\nclass Base {\n  void process(String s) {}\n  void keep(String s) {}\n  void two(int a, int b) {}\n}\n",
        "a/Derived.java": ("package a;\nclass Derived extends Base {\n  void process(int n) {}\n  void keep(String other) {}\n  void two(int a) {}\n"
                           "  void test() {\n    process(\"hello\");\n    keep(\"x\");\n    two(1, 2);\n    two(1);\n  }\n}\n"),
    }
    path = "a/Derived.java"
    both = answers(files, path, "process")          # one argument fits both: which one depends on its type, which is not read
    assert sorted(both[2]) == [("a/Base.java", "Base.process"), ("a/Derived.java", "Derived.process")]
    assert answers(files, path, "keep") == ("L0", "syntactic", [("a/Derived.java", "Derived.keep")])       # an override: its own
    assert answers(files, path, "two") == ("L1", "import-resolved", [("a/Base.java", "Base.two")])          # two arguments: the base's
    assert answers(files, path, "two", nth=1) == ("L0", "syntactic", [("a/Derived.java", "Derived.two")])


def test_a_name_that_two_wildcard_imports_both_bring_is_either():
    files = {"a.py": "def ping(): pass\ndef _hidden(): pass\n", "b.py": "def ping(): pass\ndef only_b(): pass\n",
             "app.py": "from a import *\nfrom b import *\ndef main():\n    ping()\n    only_b()\n    _hidden()\n"}
    assert answers(files, "app.py", "ping") == ("L1", "import-resolved", [("a.py", "ping"), ("b.py", "ping")])
    assert answers(files, "app.py", "only_b") == ("L1", "import-resolved", [("b.py", "only_b")])
    assert answers(files, "app.py", "_hidden") is None      # a star import does not bring names that start with an underscore


def test_a_module_that_imports_a_name_and_republishes_another_of_that_name_is_not_decided():
    files = {"internal.ts": "export function run() {}\n", "public.ts": "export function run() {}\nexport function only() {}\n",
             "module.ts": "import { run } from './internal';\nexport * from './public';\nrun;\n",
             "explicit.ts": "export { run } from './internal';\nexport * from './public';\n",
             "app.ts": "import { run, only } from './module';\nimport { run as chosen } from './explicit';\nrun();\nonly();\nchosen();\n"}
    assert sorted(answers(files, "app.ts", "run")[2]) == [("internal.ts", "run"), ("public.ts", "run")]
    assert answers(files, "app.ts", "only") == ("L1", "import-resolved", [("public.ts", "only")])
    assert answers(files, "app.ts", "chosen") == ("L1", "import-resolved", [("internal.ts", "run")])   # named explicitly: it wins over the star


def test_rust_does_not_merge_two_types_of_one_name_in_one_crate():
    files = {"src/lib.rs": "mod model;\nmod other;\nmod more;\n",
             "src/model.rs": "pub trait Trait { fn f(&self) {} }\npub struct Item;\nimpl Trait for Item {}\nimpl Item { fn test(&self) { self.f(); self.g(); } }\n",
             "src/other.rs": "pub struct Item;\nimpl Item { pub fn f(&self) {} pub fn g(&self) {} }\n",
             "src/more.rs": "use crate::model::Item;\nimpl Item { pub fn g(&self) {} }\n"}
    assert answers(files, "src/model.rs", "f") == ("L0", "syntactic", [("src/model.rs", "Trait.f")])
    assert answers(files, "src/model.rs", "g") == ("L1", "import-resolved", [("src/more.rs", "Item.g")])   # the impl that names this Item


def test_an_annotation_with_arguments_before_a_method_is_not_read_as_its_parameters():
    files = {
        "a/Types.java": "package a;\ninterface Types {\n  @Query(\"SELECT t FROM T t ORDER BY t.name\")\n  @Transactional(readOnly = true)\n  java.util.List<String> findAll();\n  void keep(@Valid String one, final int two);\n}\n",
        "a/Use.java": "package a;\nclass Use {\n  private final Types types;\n  void run() { this.types.findAll(); types.keep(\"x\", 2); }\n}\n",
    }
    assert answers(files, "a/Use.java", "findAll") == ("L1", "import-resolved", [("a/Types.java", "Types.findAll")])
    assert answers(files, "a/Use.java", "keep") == ("L1", "import-resolved", [("a/Types.java", "Types.keep")])


def test_a_call_through_an_import_from_outside_is_not_answered_with_a_method_that_shares_its_name():
    files = {
        "pkg/client.py": "class Client:\n    def post(self, url):\n        pass\n",
        "pkg/use.py": "import requests\nimport requests.adapters as adapters\n\ndef send(url):\n    requests.post(url)\n    adapters.post(url)\n",
    }
    assert answers(files, "pkg/use.py", "post") is None             # requests.post, though Client.post is the tree's only `post`
    assert answers(files, "pkg/use.py", "post", nth=1) is None


# ---- found by the independent review of the branch ------------------------------------------------

def certainty(files, path, name, nth=0):
    graph, sites = build(files)
    found = edge(graph, sites, path, name, nth)
    return None if found is None else (sorted(t.qualified for t in found.targets), found.certain)


def test_a_method_inherited_past_a_base_from_outside_is_not_certain():
    """The base that comes first may define it too, and nothing here can look."""
    python = {
        "app/mixins.py": "class ExportMixin:\n    def get_queryset(self):\n        pass\n    def only_here(self):\n        pass\n",
        "app/admin.py": ("from django.contrib import admin\nfrom .mixins import ExportMixin\n\n"
                         "class MyAdmin(admin.ModelAdmin, ExportMixin):\n    def run(self):\n        self.get_queryset()\n        self.own()\n"
                         "    def own(self):\n        return super().get_queryset()\n\n"
                         "class Other(ExportMixin, admin.ModelAdmin):\n    def run(self):\n        self.get_queryset()\n"),
    }
    assert certainty(python, "app/admin.py", "get_queryset") == (["ExportMixin.get_queryset"], False)
    assert certainty(python, "app/admin.py", "get_queryset", nth=1) == (["ExportMixin.get_queryset"], False)    # super()
    assert certainty(python, "app/admin.py", "own") == (["MyAdmin.own"], True)                    # its own: nothing comes before
    assert certainty(python, "app/admin.py", "get_queryset", nth=2) == (["ExportMixin.get_queryset"], True)   # the mixin is first
    go = {"go.mod": "module x\n", "p/p.go": "package p\n\nimport \"sync\"\n\ntype Deep struct{}\nfunc (Deep) Lock() {}\ntype Mid struct{ Deep }\n"
                                            "type S struct {\n\tsync.Mutex\n\tMid\n}\nfunc (s *S) Test() { s.Lock() }\n"}
    assert certainty(go, "p/p.go", "Lock") == (["Deep.Lock"], False)
    java = {"a/Iface.java": "package a;\ninterface Iface {\n  default void close() {}\n}\n",
            "a/C.java": "package a;\nimport java.io.InputStream;\nclass C extends InputStream implements Iface {\n  void run() { close(); this.close(); }\n}\n"}
    assert certainty(java, "a/C.java", "close") == (["Iface.close"], False)
    assert certainty(java, "a/C.java", "close", nth=1) == (["Iface.close"], False)


def test_a_signature_too_long_to_be_kept_whole_does_not_decide_between_overloads():
    annotation = '@RequestMapping(value = "' + "/a/very/long/path" * 16 + '", method = RequestMethod.POST, produces = "application/json")'
    assert len(annotation) > 300
    files = {"a/Base.java": "package a;\nclass Base {\n  String save(String s) { return s; }\n}\n",
             "a/Ctl.java": "package a;\nclass Ctl extends Base {\n  " + annotation + "\n  public String save(String s) { return s; }\n  void run() { save(\"x\"); }\n}\n"}
    targets, certain = certainty(files, "a/Ctl.java", "save")
    assert "Ctl.save" in targets and not (targets == ["Base.save"])         # never the base alone: the override is right there
    alone = {"a/Ctl.java": files["a/Ctl.java"].replace(" extends Base", "")}
    assert certainty(alone, "a/Ctl.java", "save") == (["Ctl.save"], True)
    many = ", ".join(f"String p{n}" for n in range(40))
    wide = {"a/Ctl.java": "package a;\nclass Ctl {\n  void save(" + many + ") {}\n  void run() { save(" + ", ".join(['"x"'] * 40) + "); }\n}\n"}
    assert certainty(wide, "a/Ctl.java", "save") == (["Ctl.save"], True)


def test_overrides_are_recognised_through_annotations_and_cpp_defaults_do_not_narrow():
    java = {"a/Base.java": "package a;\nclass Base {\n  void keep(String s) {}\n}\n",
            "a/D.java": "package a;\nclass D extends Base {\n  void keep(@javax.validation.Valid String s) {}\n  void run() { keep(\"x\"); }\n}\n"}
    assert certainty(java, "a/D.java", "keep") == (["D.keep"], True)
    cpp = {"base.h": "struct Base { void save(int a); };\n",
           "d.cpp": "#include \"base.h\"\nstruct D : Base { void save(int a, int b = 0); void run() { save(1); } };\n"}
    found = certainty(cpp, "d.cpp", "save")
    assert found is not None and "D.save" in found[0]                      # a default argument: one argument does call it


def test_a_value_built_by_a_name_the_function_binds_itself_has_no_known_type():
    files = {
        "pkg/store.py": "class Store:\n    def save(self):\n        pass\n\nclass Other:\n    def save(self):\n        pass\n\ndef make_store() -> Store:\n    return Store()\n",
        "pkg/use.py": ("from .store import make_store\n\ndef f(make_store):\n    s = make_store()\n    s.save()\n\n"
                       "def g(kind, registry):\n    make_store = registry[kind]\n    s = make_store()\n    s.save()\n\n"
                       "class Service:\n    def __init__(self, make_store):\n        self.store = make_store()\n    def run(self):\n        self.store.save()\n"),
    }
    for nth in range(3):
        found = certainty(files, "pkg/use.py", "save", nth)
        assert found is None or found[1] is False                          # the parameter is not the imported factory
    rust = {"src/lib.rs": ("struct A;\nimpl A { fn go(&self) {} }\nstruct B;\nimpl B { fn go(&self) {} }\nfn make() -> A { A }\n"
                           "mod tests {\n    use super::*;\n    fn make() -> B { B }\n    fn t() { let s = make(); s.go(); }\n}\n")}
    assert certainty(rust, "src/lib.rs", "go") == (["B.go"], True)          # the inline module's own `make`


def test_a_type_parameter_or_a_rebound_name_is_not_a_type_of_these_files():
    go = {"go.mod": "module x\n", "p/a.go": "package p\n\nfunc F[T any](x T) { x.Lock() }\n", "p/b.go": "package p\n\ntype T struct{}\nfunc (t T) Lock() {}\n"}
    found = certainty(go, "p/a.go", "Lock")
    assert found is None or found[1] is False
    ts = {"a.ts": "class T { save() {} }\nclass Repo<T> {\n  private kept: T;\n  run(item: T) { item.save(); this.kept.save(); }\n}\n"}
    for nth in (0, 1):
        found = certainty(ts, "a.ts", "save", nth)
        assert found is None or found[1] is False
    alias = {"pkg/store.py": "class Store:\n    def save(self):\n        pass\nclass Cache:\n    def save(self):\n        pass\n",
             "pkg/use.py": "from .store import Store, Cache\n\nStore = Cache\n\ndef f(s: Store):\n    s.save()\n"}
    found = certainty(alias, "pkg/use.py", "save")
    assert found is None or found[1] is False


def test_a_package_that_names_both_its_code_and_its_declarations_is_entered_by_its_code():
    files = {"packages/x/package.json": '{"name": "x", "main": "x.js", "types": "x.d.ts"}',
             "packages/x/x.d.ts": "export declare class X { run(): void; }\n",
             "packages/x/x.js": "class X { run() {} }\nmodule.exports = { X };\n",
             "app.js": "const { X } = require('x');\nnew X();\n"}
    assert answers(files, "app.js", "X") == ("L1", "import-resolved", [("packages/x/x.js", "X")])


def test_a_value_built_by_a_call_that_is_only_guessed_has_no_known_type():
    files = dict(BUILT)
    files["pkg/late.py"] = "from .plugins import registry\n\ndef f():\n    built = registry.build()\n    built.save()\n"
    files["pkg/plugins.py"] = "registry = object()\n"
    # `registry.build()` is answered by name (Factory.build is the only `build`), so what it returns is not known.
    assert answers(files, "pkg/late.py", "save") == ("L1", "name", [("pkg/store.py", "Store.save"), ("pkg/store.py", "Other.save")])


INNER = ("def outer(passed):\n"
         "    def helper(x):\n"
         "        return x\n"
         "    def again():\n"
         "        return helper(2)\n"
         "    helper(1)\n"
         "    passed(3)\n"
         "    return again\n"
         "def sibling():\n"
         "    def helper(y):\n"
         "        return y\n"
         "    return helper(4)\n"
         "def stranger():\n"
         "    return helper(5)\n")


def test_a_call_to_an_inner_function_reaches_it_and_only_from_inside_its_function():
    graph, sites = build({"a.py": INNER})
    first = edge(graph, sites, "a.py", "helper", 0)                    # from a function inside `outer`
    assert summary(first) == ("L0", "syntactic", [("a.py", "outer.helper")]) and first.certain
    assert [(t.start_line, t.end_line, t.kind) for t in first.targets] == [(2, 3, "function")]
    assert summary(edge(graph, sites, "a.py", "helper", 1)) == ("L0", "syntactic", [("a.py", "outer.helper")])
    own = edge(graph, sites, "a.py", "helper", 2)                      # `sibling` has its own
    assert summary(own) == ("L0", "syntactic", [("a.py", "sibling.helper")]) and own.targets[0].start_line == 10
    assert edge(graph, sites, "a.py", "helper", 3) is None             # `stranger` sees neither
    assert edge(graph, sites, "a.py", "passed") is None                # a parameter is still nobody's function


def test_an_inner_function_under_a_name_assigned_again_or_defined_twice_is_not_certain():
    script = ("function outer(cb, flag) {\n"
              "  let later = function () {};\n"
              "  later = cb;\n"
              "  later();\n"
              "  const fixed = () => 1;\n"
              "  fixed();\n"
              "}\n")
    graph, sites = build({"a.js": script})
    doubtful, sure = edge(graph, sites, "a.js", "later"), edge(graph, sites, "a.js", "fixed")
    assert summary(doubtful) == ("L0", "syntactic", [("a.js", "outer.later")]) and not doubtful.certain and "assigned again" in doubtful.doubt
    assert sure.certain
    twice = ("def outer(flag):\n    if flag:\n        def step(): return 1\n    else:\n        def step(): return 2\n    return step()\n")
    graph, sites = build({"a.py": twice})
    both = edge(graph, sites, "a.py", "step")
    assert [t.start_line for t in both.targets] == [3, 5] and not both.certain


def test_a_plain_call_beside_a_local_class_with_a_method_of_that_name_is_the_file_s_function():
    source = ("def shout(): ...\n"
              "def outer():\n"
              "    class C:\n"
              "        def shout(self):\n"
              "            return 1\n"
              "    return shout()\n")
    found = edge(*build({"a.py": source}), "a.py", "shout")
    assert summary(found) == ("L0", "syntactic", [("a.py", "shout")]) and found.targets[0].start_line == 1


def test_a_rust_function_that_is_also_imported_under_the_same_name_is_one_of_two_not_certain():
    """Found on a corpus never seen before (rayon): a fallback under `#[cfg(not(unix))]` beside `pub use self::unix::f` under
    `#[cfg(unix)]`. Rust allows a function and an import of one name in one module only where the conditions exclude each
    other, and which holds is the platform's to say: both are offered."""
    rust = {"Cargo.toml": '[package]\nname = "demo"\n',
            "src/cpu_time/mod.rs": ("#[cfg(unix)]\nmod unix;\n#[cfg(unix)]\npub use self::unix::get_cpu_time;\n\n"
                                    "#[cfg(not(unix))]\npub fn get_cpu_time() -> Option<u64> {\n    None\n}\n\n"
                                    "pub fn measure() -> Option<u64> {\n    get_cpu_time()\n}\n"),
            "src/cpu_time/unix.rs": "pub fn get_cpu_time() -> Option<u64> {\n    Some(1)\n}\n",
            "src/lib.rs": "mod cpu_time;\n"}
    found, certain = certainty(rust, "src/cpu_time/mod.rs", "get_cpu_time")
    assert sorted(found) == ["get_cpu_time", "get_cpu_time"] and certain is False
    graph, sites = build(rust, FEATURES)
    assert sorted(target.path for target in edge(graph, sites, "src/cpu_time/mod.rs", "get_cpu_time").targets) == ["src/cpu_time/mod.rs", "src/cpu_time/unix.rs"]
    # a function beside an import of a *module* of its name is no such case: the two names live apart
    apart = {"Cargo.toml": '[package]\nname = "demo"\n', "src/lib.rs": "mod parse;\nmod run;\n", "src/parse.rs": "pub fn tokens() {}\n",
             "src/run.rs": "use crate::parse;\n\nfn parse() {}\n\nfn go() {\n    parse();\n    parse::tokens();\n}\n"}
    assert certainty(apart, "src/run.rs", "parse") == (["parse"], True)


def test_a_rust_call_written_through_a_trait_is_not_certain_of_the_trait_s_own_method():
    """Found on rayon: `Latch::set(&self.latch)`. The callee is the implementation for the value's type; the trait's
    method is where it is declared. It is still the answer offered, and no longer stamped certain."""
    rust = {"Cargo.toml": '[package]\nname = "demo"\n', "src/lib.rs": "mod latch;\nmod registry;\n",
            "src/latch.rs": ("pub trait Latch {\n    unsafe fn set(this: *const Self);\n}\n\npub struct LockLatch;\n\n"
                             "impl Latch for LockLatch {\n    unsafe fn set(this: *const Self) {}\n}\n\n"
                             "impl LockLatch {\n    pub fn new() -> LockLatch {\n        LockLatch\n    }\n}\n"),
            "src/registry.rs": ("use crate::latch::{Latch, LockLatch};\n\nfn wake(latch: &LockLatch) {\n    unsafe { Latch::set(latch) };\n"
                                "    let fresh = LockLatch::new();\n}\n")}
    found, certain = certainty(rust, "src/registry.rs", "set")
    assert found == ["Latch.set"] and certain is False
    assert certainty(rust, "src/registry.rs", "new") == (["LockLatch.new"], True)       # through a type, as before
    same_file = {"Cargo.toml": '[package]\nname = "demo"\n',
                 "src/lib.rs": ("trait Shape {\n    fn area(&self) -> f64;\n}\nstruct Dot;\nimpl Shape for Dot {\n    fn area(&self) -> f64 { 0.0 }\n}\n"
                                "fn total(dot: &Dot) -> f64 {\n    Shape::area(dot)\n}\n")}
    assert certainty(same_file, "src/lib.rs", "area")[1] is False


def test_a_trait_reached_by_a_path_is_a_trait_all_the_same():
    rust = {"Cargo.toml": '[package]\nname = "demo"\n', "src/lib.rs": "mod latch;\nmod scope;\n",
            "src/latch.rs": ("pub trait Latch {\n    unsafe fn set(this: *const Self);\n}\n\npub struct LockLatch;\n\n"
                             "impl Latch for LockLatch {\n    unsafe fn set(this: *const Self) {}\n}\n\npub fn make() -> LockLatch {\n    LockLatch\n}\n"),
            "src/scope/mod.rs": ("use crate::latch::LockLatch;\n\nfn wake(latch: &LockLatch) {\n    unsafe { crate::latch::Latch::set(latch) };\n"
                                 "    let other = crate::latch::make();\n}\n")}
    assert certainty(rust, "src/scope/mod.rs", "set") == (["Latch.set"], False)
    assert certainty(rust, "src/scope/mod.rs", "make") == (["make"], True)             # a function by its path: as before


def test_a_rust_function_beside_an_import_of_something_that_is_not_a_function_stays_certain():
    """Macros, and types, are named apart from functions: `use other::log;` (a macro) beside `fn log()` needs no `#[cfg]`."""
    macro = {"Cargo.toml": '[package]\nname = "demo"\n', "src/lib.rs": "mod other;\nmod run;\n",
             "src/other.rs": "#[macro_export]\nmacro_rules! report {\n    () => {};\n}\npub(crate) use report;\n",
             "src/run.rs": "use crate::other::report;\n\nfn report() {}\n\nfn go() {\n    report();\n}\n"}
    assert certainty(macro, "src/run.rs", "report") == (["report"], True)
    typed = {"Cargo.toml": '[package]\nname = "demo"\n', "src/lib.rs": "mod run;\n",
             "src/run.rs": "use std::fmt::Formatter;\n\n#[allow(non_snake_case)]\nfn Formatter() {}\n\nfn go() {\n    Formatter();\n}\n"}
    assert certainty(typed, "src/run.rs", "Formatter") == (["Formatter"], True)        # an imported name written as a type is a type
    outside = {"Cargo.toml": '[package]\nname = "demo"\n', "src/lib.rs": "mod run;\n",
               "src/run.rs": "#[cfg(unix)]\nuse libc::getpid;\n\n#[cfg(not(unix))]\nfn getpid() -> i32 {\n    0\n}\n\nfn go() {\n    getpid();\n}\n"}
    assert certainty(outside, "src/run.rs", "getpid") == (["getpid"], False)           # the other one is outside these files: still not certain


def test_a_method_given_to_a_built_in_type_is_not_certainly_what_its_own_call_reaches():
    """Found on rayon: `impl ChunkBySlice<T> for &[T] { fn chunk_by(self, …) { self.chunk_by(…) } }`. A slice has a
    `chunk_by` of its own, and a type's own method comes before a trait's: the call reaches the standard library, not
    the method it stands in. The built-in type's methods are not among these files, so the answer is not certain."""
    rust = {"Cargo.toml": '[package]\nname = "demo"\n',
            "src/lib.rs": ("trait ChunkBy<T> {\n    fn chunk_by(self, size: usize) -> usize;\n    fn halves(self) -> usize;\n}\n\n"
                           "impl<T> ChunkBy<T> for &[T] {\n    fn chunk_by(self, size: usize) -> usize {\n        self.chunk_by(size)\n    }\n"
                           "    fn halves(self) -> usize {\n        self.chunk_by(2)\n    }\n}\n\n"
                           "impl<T> ChunkBy<T> for &mut [T] {\n    fn chunk_by(self, size: usize) -> usize {\n        size\n    }\n"
                           "    fn halves(self) -> usize {\n        self.chunk_by(2)\n    }\n}\n\n"
                           "impl ChunkBy<u8> for u64 {\n    fn chunk_by(self, size: usize) -> usize {\n        size\n    }\n"
                           "    fn halves(self) -> usize {\n        self.chunk_by(2)\n    }\n}\n\n"
                           "struct Mine;\nimpl Mine {\n    fn step(&self) {}\n    fn run(&self) {\n        self.step()\n    }\n}\n")}
    for nth in range(4):                             # in the slice's impl (twice), the mutable slice's and the integer's
        found, certain = certainty(rust, "src/lib.rs", "chunk_by", nth)
        assert len(found) == 1 and found[0].endswith(".chunk_by") and certain is False, nth
    assert certainty(rust, "src/lib.rs", "step") == (["Mine.step"], True)               # a type of these files: as before
