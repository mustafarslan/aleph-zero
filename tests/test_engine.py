import shutil
import time
from pathlib import Path

import apsw
import pytest

from aleph_zero import engine as engine_module
from aleph_zero import gitio
from aleph_zero.engine import Engine

SAMPLES = Path(__file__).parent / "samples"


def paths(answer):
    return [hit.path for hit in answer.hits]


def test_search_symbol_outline_and_status_through_one_engine(repo):
    engine = Engine(default_cwd=repo.root)
    assert paths(engine.search("render_invoice"))[0] == "src/billing/invoice.py"
    found = engine.symbol("refresh_token")
    assert [(s.path, s.line) for s in found.symbols] == [("src/auth/token_store.py", 4)]
    assert engine.outline("docs/guide.md").items[0].name == "Guide"
    status = engine.status()
    assert status["root"] == str(repo.root) and status["files_in_view"] == 3 and status["pending"] == 0
    assert status["schema_version"] >= 3 and status["chunker_version"] >= 3 and status["untracked_cache"] is False
    assert status["symbols_in_view"] == 4 and status["index"].endswith("aleph-zero/index.db")


def test_two_worktrees_of_one_repository_keep_separate_views(repo, tmp_path):
    other = tmp_path / "feature-tree"
    repo.git("worktree", "add", "-q", "-b", "feature", str(other))
    (other / "src/feature_only.py").write_text("def quokka_feature():\n    return 1\n")
    engine = Engine(default_cwd=repo.root)
    assert paths(engine.search("quokka_feature", cwd=other)) == ["src/feature_only.py"]
    assert paths(engine.search("quokka_feature")) == []
    assert paths(engine.search("quokka_feature", cwd=other / "src")) == ["src/feature_only.py"]  # any directory inside
    assert engine.status(cwd=other)["root"] == str(other.resolve())
    assert engine.status()["index"] == engine.status(cwd=other)["index"]  # one index, two views


def test_lang_narrows_the_search_to_a_language(repo):
    for sample in SAMPLES.iterdir():
        target = repo.root / "lib" / sample.name
        target.parent.mkdir(exist_ok=True)
        shutil.copy(sample, target)
    engine = Engine(default_cwd=repo.root)
    assert set(paths(engine.search("TokenStore", lang="go", k=20))) == {"lib/sample.go"}
    assert set(paths(engine.search("TokenStore", lang="ts", k=20))) == {"lib/sample.ts"}
    assert set(paths(engine.search("TokenStore", lang="cpp", path_glob="lib/*", k=20))) == {"lib/sample.cpp"}
    with pytest.raises(ValueError, match="unknown language"):
        engine.search("TokenStore", lang="cobol")


def test_a_cold_index_under_a_time_limit_answers_in_time_and_says_it_is_partial(repo):
    for i in range(400):
        repo.write(f"gen/module_{i}.py", "".join(f"def function_{i}_{j}():\n    return {j}\n\n\n" for j in range(30)))
    repo.commit("many files")
    engine = Engine(default_cwd=repo.root)
    started = time.monotonic()
    first = engine.search("function_399_29", timeout_ms=150)
    assert time.monotonic() - started < 0.3
    assert first.timed_out and first.pending > 0
    later = engine.search("function_399_29")  # no limit: the index is finished and the answer is whole
    assert not later.timed_out and later.pending == 0
    assert (later.hits[0].path, later.hits[0].symbol) == ("gen/module_399.py", "function_399_29")


def test_a_query_that_runs_past_its_deadline_is_interrupted(repo, monkeypatch):
    engine = Engine(default_cwd=repo.root)
    engine.search("render_invoice")

    def endless(index, view, query, **options):
        index.conn.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c").fetchall()

    monkeypatch.setattr(engine_module, "run_search", endless)
    started = time.monotonic()
    answer = engine.search("render_invoice", timeout_ms=200)
    assert time.monotonic() - started < 0.4
    assert answer.timed_out and answer.hits == []
    monkeypatch.undo()
    assert paths(engine.search("render_invoice"))[0] == "src/billing/invoice.py"  # the connection is still usable


def test_git_calls_respect_the_deadline(repo):
    with pytest.raises(gitio.GitError, match="timed out"):
        with gitio.until(time.monotonic() - 1):
            gitio.repo_info(repo.root)


def test_text_shown_once_in_a_session_is_pointed_to_the_second_time(repo):
    engine = Engine(default_cwd=repo.root)
    first = engine.render(engine.search("render_invoice"))
    assert "Invoice total" in first
    second = engine.render(engine.search("render_invoice"))
    assert "(shown earlier in this session)" in second and "Invoice total" not in second
    assert "Invoice total" in engine.render(engine.search("render_invoice"), detail="full")
    repo.write("src/billing/invoice.py", "def render_invoice(order):\n    return 'changed body'\n")
    assert "changed body" in engine.render(engine.search("render_invoice"))  # new text is new


def test_a_directory_that_is_not_walked_is_an_error_the_caller_can_show(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    with pytest.raises(gitio.GitError):
        Engine(default_cwd=tmp_path).search("anything")


def test_a_time_limit_is_kept_within_a_file_or_two_while_indexing(repo):
    body = "".join(f"def function_{j}(value):\n    return value + {j}  # {'x' * 60}\n\n\n" for j in range(900))
    for i in range(120):
        repo.write(f"big/module_{i}.py", body.replace("function_", f"function_{i}_"))
    repo.commit("large files")
    engine = Engine(default_cwd=repo.root)
    started = time.monotonic()
    answer = engine.search("function_5_5", timeout_ms=300)
    took = time.monotonic() - started
    assert answer.timed_out and answer.pending > 0
    assert took < 0.45, f"a 300 ms limit took {took * 1000:.0f} ms"


def test_a_cancellation_stops_indexing_promptly(repo):
    import threading

    body = "".join(f"def function_{j}(value):\n    return value + {j}  # {'x' * 60}\n\n\n" for j in range(900))
    for i in range(120):
        repo.write(f"big/module_{i}.py", body.replace("function_", f"function_{i}_"))
    repo.commit("large files")
    engine = Engine(default_cwd=repo.root)
    outcome = {}

    def run():
        try:
            engine.search("function_5_5")
            outcome["result"] = "answered"
        except engine_module.Cancelled:
            outcome["result"] = "cancelled"
        outcome["at"] = time.monotonic()

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.2)
    asked = time.monotonic()
    engine.cancel()
    worker.join(timeout=10)
    assert outcome["result"] == "cancelled" and outcome["at"] - asked < 0.4
    engine.resume()
    assert engine.search("function_5_5").hits  # and the engine still works


def test_building_the_view_of_a_very_dirty_tree_respects_the_time_limit(repo):
    for i in range(3000):
        repo.write(f"scratch/file_{i}.txt", f"untracked {i}\n" * 200)
    engine = Engine(default_cwd=repo.root)
    started = time.monotonic()
    answer = engine.search("untracked", timeout_ms=60)
    assert time.monotonic() - started < 0.25 and answer.timed_out


def test_a_collection_that_lands_between_the_check_and_the_search_does_not_cost_the_answer(repo, monkeypatch):
    """Another session collects this session's content after the index was found complete
    and before the search reads it. The answer must still be right."""
    import time

    from aleph_zero import engine as engine_module
    from aleph_zero.indexer import collect_garbage
    from aleph_zero.store import index_path, open_db

    engine = Engine(repo.root)
    assert engine.search("render invoice").hits
    real, fired = engine_module.run_search, []

    def racing(*args, **kwargs):
        if not fired:
            fired.append(collect_garbage(open_db(index_path(repo.root)), max_age_days=0, now=time.time() + 10))
        return real(*args, **kwargs)

    monkeypatch.setattr(engine_module, "run_search", racing)
    answer = engine.search("render invoice")
    assert fired and fired[0]["blobs_removed"] == 3
    assert [hit.path for hit in answer.hits][:1] == ["src/billing/invoice.py"]
    assert engine.symbol("refresh_token").symbols[0].path == "src/auth/token_store.py"


def test_the_engine_collects_old_content_after_indexing_when_it_is_due(repo):
    import time

    engine = Engine(repo.root)
    engine.search("refresh token")
    index = next(iter(engine._indexes.values()))
    repo.write("src/billing/invoice.py", "replaced = True\n")
    repo.commit("replace")
    engine.search("replaced")
    index.conn.execute("UPDATE blobs SET last_seen_at = ? WHERE (blob_oid, family) NOT IN "
                       "(SELECT blob_oid, family FROM temp.view_blobs)", (int(time.time() - 40 * 86400),))
    index.conn.execute("UPDATE meta SET value = value - 90000 WHERE key = 'collected_at'")
    before = index.conn.execute("SELECT count(*) FROM blobs").fetchall()[0][0]
    engine.search("replaced")                    # nothing was stored: no collection
    assert index.conn.execute("SELECT count(*) FROM blobs").fetchall()[0][0] == before
    repo.write("src/new_file.py", "def brand_new():\n    return 1\n")
    engine.search("brand_new", timeout_ms=5000)  # stored something, but the caller set a time limit
    assert index.conn.execute("SELECT count(*) FROM blobs").fetchall()[0][0] == before + 1
    repo.write("src/newer_file.py", "def even_newer():\n    return 1\n")
    assert engine.search("even_newer").hits      # stored something, no limit: old content goes
    assert index.conn.execute("SELECT count(*) FROM blobs").fetchall()[0][0] == before + 1
