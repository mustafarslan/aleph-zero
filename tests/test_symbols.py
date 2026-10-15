import json
import shutil
import time
from pathlib import Path

from aleph_zero import cli, symbols
from aleph_zero.indexer import Index
from aleph_zero.search import search
from aleph_zero.store import index_path, open_db
from aleph_zero.view import build_view

SAMPLES = Path(__file__).parent / "samples"


def _ready(repo):
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    return index, view


def _with_samples(repo):
    for sample in SAMPLES.iterdir():
        target = repo.root / "lib" / sample.name
        target.parent.mkdir(exist_ok=True)
        shutil.copy(sample, target)
    return _ready(repo)


def test_a_definition_is_found_by_its_name(repo):
    index, view = _ready(repo)
    hit, = symbols.lookup(index, view, "refresh_token")
    assert (hit.path, hit.line, hit.kind, hit.qualified) == ("src/auth/token_store.py", 4, "function", "refresh_token")
    assert hit.signature == "def refresh_token(session)"
    constant, = symbols.lookup(index, view, "MAX_RETRY_BACKOFF_EXPONENT")
    assert constant.kind == "constant" and constant.line == 1


def test_a_qualified_name_narrows_the_lookup(repo):
    index, view = _with_samples(repo)
    names = [(hit.path, hit.qualified) for hit in symbols.lookup(index, view, "TokenStore.get")]
    assert ("lib/sample.ts", "TokenStore.get") in names and ("lib/Sample.java", "TokenStore.get") in names
    assert all(qualified.lower().endswith("tokenstore.get") for _, qualified in names)
    cpp = [hit.path for hit in symbols.lookup(index, view, "auth::TokenStore::Get")]
    assert cpp == ["lib/sample.cpp", "lib/sample.cpp"]  # the declaration and the definition


def test_lookup_ignores_case_but_prefers_the_exact_spelling(repo):
    index, view = _with_samples(repo)
    hits = symbols.lookup(index, view, "max_retry")
    assert {hit.name for hit in hits} == {"MAX_RETRY"}
    exact = symbols.lookup(index, view, "MaxRetry")
    assert exact[0].name == "MaxRetry" and exact[0].path == "lib/sample.go"


def test_types_and_functions_come_before_fields_of_the_same_name(repo):
    repo.write("a.py", "class Engine:\n    size = 1\n\n\ndef size():\n    return 2\n")
    index, view = _ready(repo)
    assert [hit.kind for hit in symbols.lookup(index, view, "size")] == ["function", "field"]


def test_a_symbol_on_another_branch_is_not_returned(repo):
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/feature.py", "def quokka_feature():\n    return 1\n")
    repo.commit("feature")
    index, view = _ready(repo)
    assert [hit.path for hit in symbols.lookup(index, view, "quokka_feature")] == ["src/feature.py"]
    repo.git("checkout", "-q", "main")
    view = build_view(repo.root)
    index.sync(view)
    assert symbols.lookup(index, view, "quokka_feature") == []


def test_an_exact_identifier_puts_its_definition_first(repo):
    repo.write("src/callers.py", "".join(
        f"def caller_{i}(session):\n    return refresh_token(session)  # refresh_token refresh_token\n\n\n"
        for i in range(30)))
    index, view = _ready(repo)
    hits = search(index, view, "refresh_token")
    assert hits[0].path == "src/auth/token_store.py" and hits[0].symbol == "refresh_token"
    assert "symbol" in hits[0].sources


def test_a_word_that_names_too_many_definitions_is_not_a_symbol_match_in_a_longer_query(repo):
    repo.write("src/many.py", "".join(f"class Box{i}:\n    def get(self):\n        return {i}\n\n\n" for i in range(40)))
    index, view = _ready(repo)
    assert not any("symbol" in hit.sources for hit in search(index, view, "get invoice total", k=20))
    assert any("symbol" in hit.sources for hit in search(index, view, "get", k=20))  # asked for by itself


def test_pieces_of_one_long_function_are_one_hit(repo):
    body = "".join(f"    total_{i} = platypus_value({i})\n" for i in range(300))
    repo.write("src/long.py", f"def long_one():\n{body}    return 0\n")
    index, view = _ready(repo)
    hits = [hit for hit in search(index, view, "platypus_value", k=20) if hit.path == "src/long.py"]
    assert len(hits) == 1
    assert (hits[0].scope_start_line, hits[0].scope_end_line) == (1, 302)
    assert hits[0].start_line >= 1 and hits[0].end_line <= 302


def test_a_hit_that_is_a_whole_definition_has_its_own_lines_as_scope(repo):
    index, view = _ready(repo)
    hit = search(index, view, "render_invoice")[0]
    assert (hit.scope_start_line, hit.scope_end_line) == (hit.start_line, hit.end_line) == (1, 3)
    assert hit.kind == "function" and hit.signature == "def render_invoice(order)"


def test_a_name_defined_two_thousand_times_is_looked_up_quickly(repo):
    for part in range(20):
        repo.write(f"gen/part_{part}.py", "".join(
            f"class Handler{part}_{i}:\n    def handle(self):\n        return {i}\n\n\n" for i in range(100)))
    index, view = _ready(repo)
    symbols.lookup(index, view, "handle", limit=20)
    started = time.perf_counter()
    hits = symbols.lookup(index, view, "handle", limit=20)
    assert time.perf_counter() - started < 0.02
    assert len(hits) == 20


def test_imports_are_stored_with_the_content(repo):
    index, view = _with_samples(repo)
    rows = index.conn.execute(
        "SELECT i.specifier, i.names, i.level, i.kind FROM imports i JOIN temp.view_blobs v "
        "ON v.blob_oid = i.blob_oid AND v.family = i.family WHERE i.family = 'python' ORDER BY i.ord").fetchall()
    assert rows == [("os", "[]", 0, "value"), ("typing", '["Optional"]', 0, "value"),
                    ("session", '["Session", "refresh"]', 1, "value")]
    counts = index.counts()
    assert counts["symbols_in_view"] > 80 and counts["imports_in_view"] >= 20


def test_symbol_command(repo, capsys):
    assert cli.main(["--cwd", str(repo.root), "symbol", "revokeAllSessions"]) == 0
    out = capsys.readouterr().out
    assert "src/auth/token_store.py:9" in out and "function" in out and "def revokeAllSessions(user)" in out
    assert cli.main(["--cwd", str(repo.root), "symbol", "render_invoice", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["symbols"][0]["path"] == "src/billing/invoice.py" and payload["symbols"][0]["line"] == 1
    assert cli.main(["--cwd", str(repo.root), "symbol", "no_such_name"]) == 0
    assert capsys.readouterr().out == 'no definition named "no_such_name" in this worktree\n'      # said, not left blank


def test_a_definition_in_the_code_comes_before_one_of_the_same_name_in_its_tests(repo):
    repo.write("tests/fakes.py", "class Manager:\n    def get_or_create(self):\n        return 'fake'\n")
    repo.write("app/models.py", "class Manager:\n    def get_or_create(self):\n        return 'real'\n")
    repo.write("spec/widget.spec.ts", "export function buildWidget() { return 1; }\n")
    repo.write("web/widget.ts", "export function buildWidget() { return 2; }\n")
    index, view = _ready(repo)
    assert [hit.path for hit in symbols.lookup(index, view, "get_or_create")] == ["app/models.py", "tests/fakes.py"]
    assert [hit.path for hit in symbols.lookup(index, view, "buildWidget")] == ["web/widget.ts", "spec/widget.spec.ts"]
    assert search(index, view, "get_or_create")[0].path == "app/models.py"


def test_a_test_framework_is_not_test_code(repo):
    repo.write("framework/test/client.py", "class Client:\n    pass\n")
    repo.write("zz/other.py", "class Client:\n    pass\n")
    repo.write("src/test/java/ClientTests.java", "class Widget { int fake; }\n")
    repo.write("zz/Widget.java", "class Widget {}\n")
    index, view = _ready(repo)
    assert [hit.path for hit in symbols.lookup(index, view, "Client")] == ["framework/test/client.py", "zz/other.py"]
    assert [hit.path for hit in symbols.lookup(index, view, "Widget")] == ["zz/Widget.java", "src/test/java/ClientTests.java"]


def test_for_a_bare_name_the_order_of_its_definitions_decides_not_their_length(repo):
    long_body = "".join(f"        step_{i} = self.prepare(defaults, {i})\n" for i in range(25))
    repo.write("app/models.py", f"class QuerySet:\n    def get_or_create(self, defaults=None):\n{long_body}        return defaults\n")
    repo.write("tests/fakes.py", "class BookManager:\n    def get_or_create(self):\n        return super().get_or_create()\n")
    for i in range(12):  # shorter chunks that match the words better than the real, long definition
        repo.write(f"app/caller_{i}.py", f"def get_or_create_for_{i}(qs):\n    return qs.get_or_create()  # get_or_create\n")
    index, view = _ready(repo)
    hits = search(index, view, "get_or_create")
    assert [hit.breadcrumb for hit in hits[:2]] == ["QuerySet.get_or_create", "BookManager.get_or_create"]


def test_in_a_longer_query_a_name_is_one_vote_among_others(repo):
    repo.write("app/total.py", "def total():\n    return 0\n")
    index, view = _ready(repo)
    hits = search(index, view, "render invoice total")
    assert hits[0].symbol == "render_invoice"  # the text match on all three words, not the function named `total`
    assert not any("symbol" in hit.sources for hit in hits)   # plain words of a sentence are not names
    shaped = search(index, view, "where is refresh_token called")
    assert shaped[0].symbol == "refresh_token" and "symbol" in shaped[0].sources
    assert "symbol" in search(index, view, "total")[0].sources  # asked for by itself, any word is a name


def test_a_cpp_destructor_is_found_by_its_tilde_name(empty_repo):
    from aleph_zero.engine import Engine

    empty_repo.write("src/buffer.cpp", (
        "class Buffer {\npublic:\n    Buffer();\n    ~Buffer();\n};\n\n"
        "Buffer::~Buffer() {\n    release();\n}\n\nBuffer::Buffer() {}\n"))
    empty_repo.commit()
    engine = Engine(empty_repo.root)
    for asked in ("~Buffer", "Buffer::~Buffer", "Buffer.~Buffer"):
        found = engine.symbol(asked).symbols
        assert found and all(hit.name == "~Buffer" for hit in found), asked
        assert any(hit.line == 7 for hit in found)
    assert all(hit.name != "~Buffer" for hit in engine.symbol("Buffer").symbols)
    assert engine.search("~Buffer").hits[0].path == "src/buffer.cpp"
