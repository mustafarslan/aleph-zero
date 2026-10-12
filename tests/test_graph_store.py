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
    assert conn.pragma("user_version") == SCHEMA_VERSION == 7
    assert conn.execute("SELECT blob_oid, chunker_version FROM blobs ORDER BY 1").fetchall() == blobs
    engine = Engine(repo.root)
    assert engine.search("refresh token").pending == 0
    assert len(engine.references("helper").callers) == 2
