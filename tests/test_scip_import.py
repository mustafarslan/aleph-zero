"""A SCIP index made elsewhere answers graph questions exactly, while its files are as they were."""
import json

import pytest

from aleph_zero import cli, scip
from aleph_zero.engine import Engine
from test_scip import document, index, occurrence

P = "scip-python python demo 0.1 "
SAVE, CACHE_SAVE, HELPER, MAIN, ALONE = (P + "app/store/Store#save().", P + "app/store/Cache#save().", P + "app/service/helper().",
                                         P + "use/main().", P + "alone/own().")
DEF = scip.ROLE_DEFINITION
STORE = "class Store:\n    def save(self, item):\n        return item\n\nclass Cache:\n    def save(self, item):\n        return item\n"
SERVICE = ("from .store import Store as S\n\n"
           "class Service:\n    def __init__(self, store: S):\n        self.store = store\n"
           "    def run(self, item, loose):\n        self.store.save(item)\n        helper(item)\n        loose.save(item)\n\n"
           "def helper(item):\n    return item\n")
USE = "from app.service import helper as h\n\ndef main():\n    h(1)\n"
ALONE_PY = "def own():\n    return 1\n\ndef caller():\n    return own()\n"


def made_index() -> bytes:
    """What a compiler would say of the four files: it knows `loose.save` is Cache's, which the resolver cannot."""
    return index(
        document("src/app/store.py", occurrence(SAVE, [1, 8, 12], DEF), occurrence(CACHE_SAVE, [5, 8, 12], DEF)),
        document("src/app/service.py", occurrence(HELPER, [10, 4, 10], DEF), occurrence(SAVE, [6, 19, 23]),
                 occurrence(HELPER, [7, 8, 14]), occurrence(CACHE_SAVE, [8, 14, 18]),
                 occurrence("scip-python python python-stdlib 3.11 builtins/print().", [7, 0, 5])),
        document("src/use.py", occurrence(MAIN, [2, 4, 8], DEF), occurrence(HELPER, [3, 4, 5]), occurrence("local 3", [3, 6, 7])),
        document("src/alone.py", occurrence(ALONE, [0, 4, 7], DEF), occurrence(ALONE, [4, 11, 14])),
        document("../outside.py", occurrence(ALONE, [0, 0, 3])),
    )


@pytest.fixture
def project(repo, tmp_path):
    repo.write("src/app/__init__.py", "")
    repo.write("src/app/store.py", STORE)
    repo.write("src/app/service.py", SERVICE)
    repo.write("src/use.py", USE)
    repo.write("src/alone.py", ALONE_PY)
    repo.commit("the app")
    made = tmp_path / "index.scip"
    made.write_bytes(made_index())
    return repo, made


def callees(repo, name="Service.run"):
    return [(r.written, r.target, r.level, r.provenance, r.certain, bool(r.downgraded))
            for r in Engine(repo.root).references(name, direction="callees").callees]


def test_an_index_is_reduced_to_references_into_the_tree_and_what_each_file_depends_on():
    kept, skipped = scip.reduce(scip.parse_index(made_index()))
    by_path = {each.path: each for each in kept}
    assert skipped == 1 and "../outside.py" not in by_path                       # a path that leaves the tree is not kept
    assert by_path["src/app/service.py"].references == [
        (6, 19, "src/app/store.py", 1, "save"), (7, 8, "src/app/service.py", 10, "helper"), (8, 14, "src/app/store.py", 5, "save")]
    assert by_path["src/app/service.py"].depends_on == ["src/app/store.py"]      # the builtin is not of this tree
    assert by_path["src/use.py"].depends_on == ["src/app/service.py"] and by_path["src/alone.py"].depends_on == []


def test_an_imported_index_answers_what_the_resolver_can_only_guess(project):
    repo, made = project
    assert callees(repo)[-2:] == [("loose.save", "Store.save", "L1", "name", False, False),
                                  ("loose.save", "Cache.save", "L1", "name", False, False)]
    report = Engine(repo.root).scip_import(made)
    assert (report["documents"], report["skipped"]) == (4, 1)
    assert callees(repo) == [
        ("self.store.save", "Store.save", "L2", "scip", True, False),
        ("helper", "helper", "L2", "scip", True, False),
        ("loose.save", "Cache.save", "L2", "scip", True, False),
    ]
    callers = Engine(repo.root).references("helper", direction="callers").callers
    assert [(r.path, r.line, r.level) for r in callers] == [("src/app/service.py", 8, "L2"), ("src/use.py", 4, "L2")]


def test_a_changed_file_sends_itself_and_whatever_depends_on_it_back_to_the_resolver(project):
    repo, made = project
    engine = Engine(repo.root)
    engine.scip_import(made)
    repo.write("src/app/store.py", STORE + "\n# edited, not committed\n")
    # service.py depends on store.py; use.py depends on service.py and so, through it, on store.py.
    assert callees(repo) == [
        ("self.store.save", "Store.save", "L1", "import-resolved", True, True),
        ("helper", "helper", "L0", "syntactic", True, True),
        ("loose.save", "Store.save", "L1", "name", False, True),
        ("loose.save", "Cache.save", "L1", "name", False, True),
    ]
    answer = engine.references("helper", direction="callers")
    assert [(r.path, r.level, bool(r.downgraded)) for r in answer.callers] == [("src/app/service.py", "L0", True),
                                                                                ("src/use.py", "L1", True)]
    assert "src/app/store.py" in answer.callers[1].downgraded                   # the reason names what changed
    from aleph_zero import callgraph
    assert "compiler index" in callgraph.text(answer)
    # A file that depends on none of them is still answered from the index.
    assert callees(repo, "caller") == [("own", "own", "L2", "scip", True, False)]
    # Put back as it was: the index is trusted again.
    repo.write("src/app/store.py", STORE)
    assert callees(repo)[2] == ("loose.save", "Cache.save", "L2", "scip", True, False)


def test_an_index_made_from_another_tree_is_trusted_only_where_the_files_are_the_same(project):
    repo, made = project
    repo.write("src/app/store.py", STORE + "\n# a later commit\n")
    repo.commit("store changes")
    engine = Engine(repo.root)
    engine.scip_import(made, tree="HEAD~1")             # the index was made one commit ago
    assert all(row[5] for row in callees(repo))         # service.py depends on a file that has changed since
    assert callees(repo, "caller") == [("own", "own", "L2", "scip", True, False)]
    engine.scip_import(made)                            # stamped with HEAD, as if it had been made now: it replaces the other
    assert callees(repo)[2] == ("loose.save", "Cache.save", "L2", "scip", True, False)
    assert len(engine.scip_imports()) == 1


def test_an_index_made_on_the_worktree_as_it_is_can_be_stamped_so(project):
    repo, made = project
    repo.write("src/alone.py", ALONE_PY + "\n# not committed\n")
    engine = Engine(repo.root)
    engine.scip_import(made)                            # stamped with HEAD: alone.py on disk is not what HEAD holds
    assert callees(repo, "caller")[0][2] != "L2"
    engine.scip_import(made, worktree=True)
    assert callees(repo, "caller") == [("own", "own", "L2", "scip", True, False)]


def test_a_dependency_set_too_large_to_check_is_not_trusted_and_says_so(project, monkeypatch):
    from aleph_zero import scipstore

    repo, made = project
    Engine(repo.root).scip_import(made)
    monkeypatch.setattr(scipstore, "DEPENDENCY_LIMIT", 1)
    answer = Engine(repo.root).references("main", direction="callees")       # use.py -> service.py -> store.py: two to check
    assert [(r.level, bool(r.downgraded)) for r in answer.callees] == [("L1", True)]
    assert "more than 1 files" in answer.callees[0].downgraded


def test_the_command_line_imports_lists_and_forgets(project, capsys, tmp_path):
    repo, made = project
    assert cli.main(["--cwd", str(repo.root), "scip", "import", str(made)]) == 0
    assert "4 documents" in capsys.readouterr().out
    assert cli.main(["--cwd", str(repo.root), "scip", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1 and listed[0]["documents"] == 4 and listed[0]["stamped"] == "HEAD"
    broken = tmp_path / "broken.scip"
    broken.write_bytes(made_index()[:-7])
    assert cli.main(["--cwd", str(repo.root), "scip", "import", str(broken)]) == 1
    assert "not a readable SCIP index" in capsys.readouterr().err
    assert len(Engine(repo.root).scip_imports()) == 1                         # the broken one changed nothing
    assert cli.main(["--cwd", str(repo.root), "scip", "forget", "--all"]) == 0
    assert Engine(repo.root).scip_imports() == [] and callees(repo)[0][2] == "L1"


def test_what_the_compiler_places_in_a_library_gets_no_answer_though_a_name_matches(project, tmp_path):
    repo, _ = project
    library = tmp_path / "library.scip"
    # The same files, but here the compiler says `loose.save` is something no document defines.
    library.write_bytes(index(
        document("src/app/store.py", occurrence(SAVE, [1, 8, 12], DEF), occurrence(CACHE_SAVE, [5, 8, 12], DEF)),
        document("src/app/service.py", occurrence(HELPER, [10, 4, 10], DEF), occurrence(SAVE, [6, 19, 23]),
                 occurrence(HELPER, [7, 8, 14]), occurrence("scip-python python requests 2.0 requests/Session#save().", [8, 14, 18]))))
    Engine(repo.root).scip_import(library)
    answer = Engine(repo.root).references("Service.run", direction="callees")
    assert [r.written for r in answer.callees] == ["self.store.save", "helper"] and answer.unanswered == 1


def test_an_index_file_too_large_to_read_whole_is_refused(project, monkeypatch):
    from aleph_zero import scipstore

    repo, made = project
    monkeypatch.setattr(scipstore, "MAX_INDEX_BYTES", 10)
    with pytest.raises(scipstore.NotAnIndex, match="largest index read"):
        Engine(repo.root).scip_import(made)
    assert Engine(repo.root).scip_imports() == []


def test_an_index_of_schema_7_gets_the_import_tables_in_place(project):
    from aleph_zero.store import SCHEMA_VERSION, index_path, open_db

    repo, made = project
    engine = Engine(repo.root)
    engine.references("helper")
    engine.close()
    conn = open_db(index_path(repo.root))
    facts = conn.execute("SELECT count(*) FROM graph_facts").fetchall()
    for statement in ("DROP TABLE scip_documents", "DROP TABLE scip_imports"):
        conn.execute(statement)
    conn.pragma("user_version", 7)
    conn.close()
    conn = open_db(index_path(repo.root))
    assert conn.pragma("user_version") == SCHEMA_VERSION == 8
    assert conn.execute("SELECT count(*) FROM graph_facts").fetchall() == facts
    assert Engine(repo.root).scip_import(made)["documents"] == 4
