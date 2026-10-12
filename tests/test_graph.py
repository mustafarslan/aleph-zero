"""The code graph: a call resolved to definitions, with how it was found and how sure it is."""
from aleph_zero import lang
from aleph_zero.graph import FileFacts, Graph
from aleph_zero.lang import parse
from aleph_zero.lang.calls import calls
from aleph_zero.lang.definitions import definitions
from aleph_zero.lang.imports import extract_imports


def build(files: dict[str, str]):
    facts, sites = {}, {}
    for path, source in files.items():
        profile = lang.profile_for_path(path)
        data = source.encode()
        tree = parse.tree(profile, data)
        found = definitions(profile, tree, data)
        facts[path] = FileFacts(profile.name, found, extract_imports(tree.root_node, profile))
        sites[path] = calls(profile, tree, data, found)
    return Graph(facts), sites


def edge(graph, sites, path, name):
    call = next(call for call in sites[path] if call.name == name)
    return graph.resolve(path, call)


def summary(found):
    return None if found is None else (found.level, found.provenance, [(t.path, t.qualified) for t in found.targets])


PY = {
    "pkg/__init__.py": "from .core import Engine\nfrom .util import slugify\n",
    "pkg/core.py": ("from .util import slugify\nfrom . import util\n\n"
                    "class Engine:\n    def run(self):\n        self.prepare()\n        slugify('x')\n        util.clean('y')\n"
                    "        local()\n        return other.save()\n    def prepare(self):\n        pass\n\n"
                    "def local():\n    pass\n"),
    "pkg/util.py": "def slugify(text):\n    return text\n\ndef clean(text):\n    return text\n\ndef _private():\n    pass\n",
    "pkg/store.py": "class Store:\n    def save(self):\n        pass\n\nclass Cache:\n    def save(self):\n        pass\n",
    "app.py": "from pkg import Engine, slugify\n\ndef main():\n    Engine()\n    slugify('a')\n    _private()\n    nowhere()\n",
}


def test_a_call_to_something_in_the_same_file_is_certain():
    graph, sites = build(PY)
    assert summary(edge(graph, sites, "pkg/core.py", "local")) == ("L0", "syntactic", [("pkg/core.py", "local")])
    assert summary(edge(graph, sites, "pkg/core.py", "prepare")) == ("L0", "syntactic", [("pkg/core.py", "Engine.prepare")])


def test_an_imported_name_is_followed_to_the_file_that_defines_it():
    graph, sites = build(PY)
    assert summary(edge(graph, sites, "pkg/core.py", "slugify")) == ("L1", "import-resolved", [("pkg/util.py", "slugify")])
    assert summary(edge(graph, sites, "pkg/core.py", "clean")) == ("L1", "import-resolved", [("pkg/util.py", "clean")])


def test_a_name_republished_by_a_package_is_followed_through_it():
    graph, sites = build(PY)
    assert summary(edge(graph, sites, "app.py", "Engine")) == ("L1", "import-resolved", [("pkg/core.py", "Engine")])
    assert summary(edge(graph, sites, "app.py", "slugify")) == ("L1", "import-resolved", [("pkg/util.py", "slugify")])


def test_a_method_call_on_an_unknown_object_is_every_method_of_that_name_and_says_so():
    graph, sites = build(PY)
    found = edge(graph, sites, "pkg/core.py", "save")
    assert summary(found) == ("L1", "name", [("pkg/store.py", "Store.save"), ("pkg/store.py", "Cache.save")])
    assert found.confidence == 0.5                                  # two candidates: not a confident answer


def test_a_private_name_of_another_file_and_a_name_defined_nowhere_get_no_answer():
    graph, sites = build(PY)
    assert edge(graph, sites, "app.py", "_private") is None
    assert edge(graph, sites, "app.py", "nowhere") is None


def test_typescript_follows_a_relative_import_and_a_barrel_file():
    graph, sites = build({
        "src/index.ts": "export { makeUser } from './user';\n",
        "src/user.ts": "export function makeUser() { return 1; }\n",
        "src/app.ts": "import { makeUser } from './index';\nimport { helper } from './lib/helper';\nexport function run() { makeUser(); helper(); }\n",
        "src/lib/helper.ts": "export function helper() { return 2; }\n",
    })
    assert summary(edge(graph, sites, "src/app.ts", "helper")) == ("L1", "import-resolved", [("src/lib/helper.ts", "helper")])
    assert summary(edge(graph, sites, "src/app.ts", "makeUser")) == ("L1", "import-resolved", [("src/user.ts", "makeUser")])


def test_a_name_with_too_many_definitions_is_not_answered():
    files = {f"m{n}.py": "class C:\n    def save(self):\n        pass\n" for n in range(25)}
    files["use.py"] = "def f(x):\n    x.save()\n"
    graph, sites = build(files)
    assert edge(graph, sites, "use.py", "save") is None
