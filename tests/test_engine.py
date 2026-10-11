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
    assert time.monotonic() - started < 1.0
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
    assert time.monotonic() - started < 1.5
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


def test_outside_a_repository_is_an_error_the_caller_can_show(tmp_path):
    with pytest.raises(gitio.GitError):
        Engine(default_cwd=tmp_path).search("anything")
