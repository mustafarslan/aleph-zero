"""What the code graph reads is stored with each blob: once, and gone when the blob is."""
from aleph_zero.engine import Engine
from aleph_zero.indexer import Index, collect_garbage
from aleph_zero.store import SCHEMA_VERSION, index_path, open_db
from aleph_zero.view import build_view

SERVICE = ("from .store import Store as S\n\n"
           "class Service(Base):\n    def __init__(self, store: S):\n        self.store = store\n"
           "    def run(self, item, make):\n        self.store.save(item)\n        helper(item)\n        make()\n\n"
           "def helper(item):\n    return item\n")


def synced(repo):
    repo.write("src/app/service.py", SERVICE)
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    return index, view


def of_blob(index, view, table, columns):
    oid = view.files["src/app/service.py"]
    return index.conn.execute(f"SELECT {columns} FROM {table} WHERE blob_oid = ? ORDER BY ord", (oid,)).fetchall()


def test_a_files_calls_are_stored_as_written_with_the_definition_they_sit_in(repo):
    index, view = synced(repo)
    symbols = {ord_: qualified for ord_, qualified in of_blob(index, view, "symbols", "ord, qualified")}
    stored = [(name, receiver, line, symbols.get(caller), via, head, receiver_type)
              for name, receiver, line, caller, via, head, receiver_type
              in of_blob(index, view, "calls", "name, receiver, line, caller, via, head, receiver_type")]
    assert stored == [
        ("save", "self.store", 7, "Service.run", "member", "self", ""),
        ("helper", "", 8, "Service.run", "", "", ""),
    ]                                           # `make()` calls a parameter: nothing could ever answer it, so it is not kept


def test_a_files_import_bindings_and_type_facts_are_stored(repo):
    index, view = synced(repo)
    assert of_blob(index, view, "bindings", "local, source, name, level, kind") == [("S", "store", "Store", 1, "import")]
    assert of_blob(index, view, "type_parts", "owner, field, stated") == [("Service", "", "Base"), ("Service", "store", "S")]
    ends = dict(of_blob(index, view, "symbols", "qualified, end_line"))
    assert ends["Service"] == 9 and ends["helper"] == 12
    assert index.counts()["calls_in_view"] >= 2


def test_indexing_a_blob_again_does_not_double_what_is_stored_for_it(repo):
    index, view = synced(repo)
    before = {table: index.conn.execute(f"SELECT count(*) FROM {table}").fetchall() for table in ("calls", "bindings", "type_parts")}
    open_db(index_path(repo.root)).execute("UPDATE blobs SET chunker_version = chunker_version - 1")
    assert index.sync(build_view(repo.root)).indexed == 4
    assert {table: index.conn.execute(f"SELECT count(*) FROM {table}").fetchall() for table in before} == before


def test_a_blob_that_is_collected_takes_its_graph_rows_with_it(repo):
    index, view = synced(repo)
    oid = view.files["src/app/service.py"]
    (repo.root / "src/app/service.py").unlink()
    index.sync(build_view(repo.root))
    collect_garbage(index.conn, max_age_days=0, now=2**40)
    for table in ("calls", "bindings", "type_parts"):
        assert index.conn.execute(f"SELECT count(*) FROM {table} WHERE blob_oid = ?", (oid,)).fetchall() == [(0,)]


def test_an_index_of_schema_6_gets_the_graph_tables_and_fills_them_on_the_next_sync(repo):
    """Schema 6 had no calls, bindings or type facts, and its blobs were written by the chunker before them."""
    repo.write("src/app/service.py", SERVICE)
    engine = Engine(repo.root)
    assert engine.search("refresh token").hits
    engine.close()
    path = index_path(repo.root)
    conn = open_db(path)
    blobs = conn.execute("SELECT count(*) FROM blobs").fetchall()
    for statement in ("DROP TABLE calls", "DROP TABLE bindings", "DROP TABLE type_parts", "ALTER TABLE symbols DROP COLUMN end_line",
                      "UPDATE blobs SET chunker_version = 4"):
        conn.execute(statement)
    conn.pragma("user_version", 6)
    conn.close()
    conn = open_db(path)
    assert conn.pragma("user_version") == SCHEMA_VERSION == 7
    assert conn.execute("SELECT count(*) FROM blobs").fetchall() == blobs
    assert conn.execute("SELECT count(*) FROM calls").fetchall() == [(0,)]
    engine = Engine(repo.root)
    assert engine.search("refresh token").pending == 0
    assert conn.execute("SELECT count(*) FROM calls").fetchall()[0][0] >= 2
    assert conn.execute("SELECT min(end_line) FROM symbols").fetchall()[0][0] >= 1
