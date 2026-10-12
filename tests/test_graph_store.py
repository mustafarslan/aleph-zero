"""The code graph of a view: facts read per blob when first asked, and the questions asked of them."""
from aleph_zero.engine import Engine
from aleph_zero.indexer import collect_garbage
from aleph_zero.store import SCHEMA_VERSION, index_path, open_db

STORE = "class Store:\n    def save(self, item):\n        return item\n\nclass Cache:\n    def save(self, item):\n        return item\n"
SERVICE = ("from .store import Store as S\n\n"
           "class Service:\n    def __init__(self, store: S):\n        self.store = store\n"
           "    def run(self, item, loose):\n        self.store.save(item)\n        helper(item)\n        loose.save(item)\n\n"
           "def helper(item):\n    return item\n")
USE = "from app.service import helper as h, Service\n\ndef main():\n    h(1)\n    Service(None).run(1, None)\n"


def app(repo):
    repo.write("src/app/__init__.py", "")
    repo.write("src/app/store.py", STORE)
    repo.write("src/app/service.py", SERVICE)
    repo.write("src/use.py", USE)
    return Engine(repo.root)


def stored(repo) -> int:
    return open_db(index_path(repo.root)).execute("SELECT count(*) FROM graph_facts").fetchall()[0][0]


def shown(references):
    return [(r.path, r.line, r.written, r.caller, f"{r.target_path}:{r.target}", r.provenance, r.candidates, r.certain) for r in references]


def test_graph_facts_are_read_when_a_graph_question_is_first_asked_not_when_a_file_is_indexed(repo):
    engine = app(repo)
    assert engine.search("refresh token").pending == 0
    assert stored(repo) == 0                                    # searching pays nothing for the graph
    answer = engine.references("helper")
    assert answer.graph_pending == 0 and stored(repo) >= 4
    before = stored(repo)
    engine.references("helper")
    assert stored(repo) == before                               # and they are read once


def test_who_calls_a_function_including_under_another_name(repo):
    answer = app(repo).references("helper", direction="callers")
    assert [hit.path for hit in answer.definitions] == ["src/app/service.py"]
    assert shown(answer.callers) == [
        ("src/app/service.py", 8, "helper", "Service.run", "src/app/service.py:helper", "syntactic", 1, True),
        ("src/use.py", 4, "h", "main", "src/app/service.py:helper", "import-resolved", 1, True),
    ]
    assert answer.callees == []


def test_what_a_method_calls_with_how_sure_each_answer_is(repo):
    answer = app(repo).references("Service.run", direction="callees")
    assert shown(answer.callees) == [
        ("src/app/service.py", 7, "self.store.save", "Service.run", "src/app/store.py:Store.save", "import-resolved", 1, True),
        ("src/app/service.py", 8, "helper", "Service.run", "src/app/service.py:helper", "syntactic", 1, True),
        # `loose` has no stated type: both `save` methods are candidates, and the answer says it is by name
        ("src/app/service.py", 9, "loose.save", "Service.run", "src/app/store.py:Store.save", "name", 2, False),
        ("src/app/service.py", 9, "loose.save", "Service.run", "src/app/store.py:Cache.save", "name", 2, False),
    ]


def test_callers_of_a_method_keep_the_certain_ones_apart_from_the_guesses(repo):
    answer = app(repo).references("Store.save")
    assert shown(answer.callers) == [
        ("src/app/service.py", 7, "self.store.save", "Service.run", "src/app/store.py:Store.save", "import-resolved", 1, True),
        ("src/app/service.py", 9, "loose.save", "Service.run", "src/app/store.py:Store.save", "name", 2, False),
    ]


def test_an_edited_file_is_answered_from_what_it_holds_now(repo):
    engine = app(repo)
    assert len(engine.references("helper").callers) == 2
    repo.write("src/use.py", "def main():\n    pass\n")       # no longer calls it; not committed
    assert [r.path for r in engine.references("helper").callers] == ["src/app/service.py"]


def test_a_callee_whose_file_left_the_view_is_no_longer_an_answer(repo):
    engine = app(repo)
    assert any(r.target == "Store.save" for r in engine.references("Service.run").callees)
    (repo.root / "src/app/store.py").unlink()
    assert [r.target for r in engine.references("Service.run").callees] == ["helper"]


def test_a_blob_that_is_collected_takes_its_graph_facts_with_it(repo):
    engine = app(repo)
    engine.references("helper")
    conn = open_db(index_path(repo.root))
    oid = conn.execute("SELECT blob_oid FROM symbols WHERE name = 'helper'").fetchall()[0][0]
    (repo.root / "src/app/service.py").unlink()
    engine.search("refresh token")
    collect_garbage(conn, max_age_days=0, now=2**40)
    assert conn.execute("SELECT count(*) FROM graph_facts WHERE blob_oid = ?", (oid,)).fetchall() == [(0,)]


def test_out_of_time_the_answer_says_how_many_files_the_graph_has_not_read(repo, monkeypatch):
    from aleph_zero import callgraph

    engine = app(repo)
    engine.search("refresh token")
    monkeypatch.setattr(callgraph, "PREPARE_BATCH", 1)
    clock = iter(range(0, 10_000, 50))
    monkeypatch.setattr(callgraph, "_now", lambda: next(clock))     # each look at the clock is 50 s later
    answer = engine.references("helper", prepare_seconds=60)
    assert answer.graph_pending > 0
    monkeypatch.undo()
    assert engine.references("helper").graph_pending == 0


def test_an_index_of_schema_6_gets_the_graph_table_and_nothing_is_indexed_again(repo):
    engine = app(repo)
    assert engine.search("refresh token").hits
    engine.close()
    path = index_path(repo.root)
    conn = open_db(path)
    blobs = conn.execute("SELECT blob_oid, chunker_version FROM blobs ORDER BY 1").fetchall()
    conn.execute("DROP TABLE graph_facts")
    conn.pragma("user_version", 6)
    conn.close()
    conn = open_db(path)
    assert conn.pragma("user_version") == SCHEMA_VERSION
    assert conn.execute("SELECT blob_oid, chunker_version FROM blobs ORDER BY 1").fetchall() == blobs
    engine = Engine(repo.root)
    assert engine.search("refresh token").pending == 0
    assert len(engine.references("helper").callers) == 2


def test_the_answer_as_text_keeps_what_is_certain_apart_from_what_is_possible(repo):
    from aleph_zero import callgraph

    text = callgraph.text(app(repo).references("Store.save"))
    assert text.splitlines() == [
        "src/app/store.py:2  method  Store.save",
        "callers: 1 certain, 1 possible",
        "  src/app/service.py:7  in Service.run  self.store.save()",
        "  possible:",
        "  src/app/service.py:9  in Service.run  loose.save()  by name alone, 1 of 2: nothing states the receiver's type",
        "callees: none in this worktree",
    ]
    text = callgraph.text(app(repo).references("Service.run", direction="callees"))
    assert text.splitlines() == [
        "src/app/service.py:6  method  Service.run",
        "callees: 2 certain, 2 possible",
        "  src/app/service.py:7  self.store.save()  ->  src/app/store.py:2  Store.save",
        "  src/app/service.py:8  helper()  ->  src/app/service.py:11  helper",
        "  possible:",
        "  src/app/service.py:9  loose.save()  ->  src/app/store.py:2  Store.save  by name alone, 1 of 2: nothing states the receiver's type",
        "  src/app/service.py:9  loose.save()  ->  src/app/store.py:6  Cache.save  by name alone, 1 of 2: nothing states the receiver's type",
    ]
    assert callgraph.text(app(repo).references("nowhere_at_all")) == 'no definition named "nowhere_at_all" in this worktree'


def test_callers_and_callees_on_the_command_line(repo, capsys):
    import json

    from aleph_zero import cli

    app(repo).close()
    assert cli.main(["--cwd", str(repo.root), "callers", "helper"]) == 0
    out = capsys.readouterr().out
    assert "src/use.py:4  in main  h()" in out and "callees" not in out
    assert cli.main(["--cwd", str(repo.root), "callees", "Service.run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["target"] for item in payload["callees"]][:2] == ["Store.save", "helper"] and payload["callers"] == []


def test_callers_under_a_name_given_by_a_renaming_re_export_are_found(repo):
    repo.write("lib/__init__.py", "from .util import slugify as slug\n")
    repo.write("lib/util.py", "def slugify(text):\n    return text\n")
    repo.write("main.py", "from lib import slug\n\ndef go():\n    return slug('x')\n")
    answer = Engine(repo.root).references("slugify", direction="callers")
    assert [(r.path, r.line, r.written, r.certain) for r in answer.callers] == [("main.py", 4, "slug", True)]


def test_a_file_that_cannot_be_read_is_reported_as_not_read(repo, monkeypatch):
    from aleph_zero import callgraph

    engine = app(repo)
    engine.search("refresh token")
    monkeypatch.setattr(callgraph, "_read", lambda *args: None)
    answer = engine.references("helper")
    assert answer.graph_pending > 0 and "not read yet" in callgraph.text(answer)


def test_what_cannot_be_looked_up_or_listed_in_full_is_said(repo):
    from aleph_zero import callgraph

    for n in range(10):
        repo.write(f"many/m{n}.py", "def same_name():\n    pass\n")
    repo.write("dollar.js", "function $(x) { return x; }\n$('a');\n")
    engine = Engine(repo.root)
    assert engine.references("same_name").truncated is True          # ten definitions: eight are answered for
    assert engine.references("$", direction="callers").truncated is True   # the text index cannot look such a name up
    repo.write("fast.py", "def dumps(x):\n    pass\n")
    repo.write("slow.py", "def dumps(x):\n    pass\n")
    repo.write("use.py", "try:\n    from fast import dumps\nexcept ImportError:\n    from slow import dumps\n\ndef f():\n    dumps(1)\n")
    text = callgraph.text(engine.references("dumps", direction="callers"))
    assert "receiver" not in text and "1 of 2 candidates" in text    # two imports of one name: no receiver is involved


def test_an_answer_in_doubt_says_why(repo):
    from aleph_zero import callgraph

    repo.write("adm/mixins.py", "class ExportMixin:\n    def get_queryset(self):\n        pass\n")
    repo.write("adm/admin.py", "from django.contrib import admin\nfrom .mixins import ExportMixin\n\n"
                               "class MyAdmin(admin.ModelAdmin, ExportMixin):\n    def run(self):\n        self.get_queryset()\n")
    answer = Engine(repo.root).references("ExportMixin.get_queryset", direction="callers")
    assert [(r.path, r.line, r.candidates, r.certain) for r in answer.callers] == [("adm/admin.py", 6, 1, False)]
    assert "inherited past a base that is not among these files" in callgraph.text(answer)


def test_an_empty_file_takes_part_with_nothing_and_breaks_nothing(repo):
    """An empty `__init__.py` is stored with no facts at all; a question that goes through it must still be answered."""
    repo.write("pkg/__init__.py", "")
    repo.write("pkg/mod.py", "def f():\n    return 1\n")
    repo.write("user.py", "from pkg import mod\n\ndef user():\n    return mod.f()\n")
    answer = Engine(repo.root).references("user", direction="callees")
    assert [(r.written, r.target, r.certain) for r in answer.callees] == [("mod.f", "f", True)]
