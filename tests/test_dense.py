import json

import numpy as np
import pytest

from aleph_zero import cli
from aleph_zero.embed import worker
from aleph_zero.embed.client import EmbedClient
from aleph_zero.engine import Engine


@pytest.fixture
def dense_engine(repo, fake_worker, monkeypatch):
    """An engine with semantic search on, whose worker is the stand-in."""
    monkeypatch.setattr(worker, "available", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    return Engine(repo.root, dense=True)


def paths(answer):
    return [hit.path for hit in answer.hits]


def test_embedding_a_worktree_reports_progress_and_status_shows_coverage(dense_engine):
    dense_engine.search("anything")   # the first query indexes the worktree
    before = dense_engine.status()["vectors"]
    assert before["with_vector"] == 0 and before["chunks"] > 0 and before["enabled"] is True
    seen = []
    report = dense_engine.embed(progress=lambda done, left: seen.append((done, left)))
    assert report.remaining == 0 and report.embedded == before["chunks"] and seen
    after = dense_engine.status()["vectors"]
    assert after["with_vector"] == after["chunks"] and after["kind"].endswith("/256")


def test_a_chunk_is_found_through_its_vector_when_no_word_matches(dense_engine, fake_worker, monkeypatch):
    dense_engine.embed()
    index = dense_engine._indexes[dense_engine.default_cwd.resolve()]
    target = index.conn.execute(
        "SELECT c.breadcrumb, c.signature, c.text FROM chunks c WHERE c.symbol = 'render_invoice'").fetchall()[0]
    from aleph_zero import embedder

    exact = EmbedClient(fake_worker).embed([embedder.chunk_text(*target)], kind="document")[0]
    # The stand-in worker has no notion of meaning, so the query is given the vector of the chunk it should find.
    monkeypatch.setattr(EmbedClient, "embed", lambda self, texts, kind: np.stack([exact] * len(texts)))
    answer = dense_engine.search("zzqqxx unrelatedword")       # no word of this is anywhere in the repository
    assert answer.hits[0].symbol == "render_invoice" and answer.hits[0].sources == ("dense",)
    assert answer.dense == "used"
    assert Engine(dense_engine.default_cwd, dense=False).search("zzqqxx unrelatedword").hits == []


def test_filters_apply_to_what_vectors_propose(dense_engine, fake_worker, monkeypatch):
    dense_engine.embed()
    answer = dense_engine.search("zzqqxx unrelatedword", kind="docs", k=20)
    assert answer.hits and all(hit.kind == "doc" for hit in answer.hits)
    scoped = dense_engine.search("zzqqxx unrelatedword", path_glob="src/billing/*", k=20)
    assert scoped.hits and set(paths(scoped)) == {"src/billing/invoice.py"}


def test_without_a_worker_the_answer_is_the_lexical_one_and_says_so(dense_engine, repo, monkeypatch):
    never = dense_engine.search("render_invoice")
    assert never.dense.startswith("no chunk of this worktree has a vector yet")
    dense_engine.embed()
    monkeypatch.setattr(worker, "available", lambda: None)
    engine = Engine(repo.root, dense=True)
    answer = engine.search("render_invoice")
    plain = Engine(repo.root, dense=False).search("render_invoice")
    assert paths(answer) == paths(plain) and answer.dense == "the embedding worker is not running"
    assert "note: semantic search was not used: the embedding worker is not running" in engine.render(answer)
    assert plain.dense == "" and "semantic" not in Engine(repo.root).render(plain)


def test_partial_coverage_is_stated_in_the_answer(dense_engine):
    dense_engine.embed(budget_seconds=0.0, batch=1)
    answer = dense_engine.search("render_invoice")
    text = dense_engine.render(answer)
    total = dense_engine.status()["vectors"]["chunks"]
    assert answer.dense == "used" and f"note: semantic search covers 1 of {total} chunks so far" in text


def test_a_worker_that_fails_mid_query_does_not_fail_the_query(dense_engine, monkeypatch):
    dense_engine.embed()

    def broken(self, texts, kind):
        from aleph_zero.embed.client import Unavailable

        raise Unavailable("the worker did not answer within 2.0 s")

    monkeypatch.setattr(EmbedClient, "embed", broken)
    answer = dense_engine.search("render_invoice")
    assert answer.hits[0].path == "src/billing/invoice.py" and "did not answer" in answer.dense


def test_embed_run_and_status_commands(repo, fake_worker, monkeypatch, capsys):
    monkeypatch.setattr(worker, "available", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    assert cli.main(["--cwd", str(repo.root), "embed", "run"]) == 0
    out = capsys.readouterr().out
    assert "embedded" in out and "0 chunks left" in out
    assert cli.main(["--cwd", str(repo.root), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["vectors"]["with_vector"] == status["vectors"]["chunks"] > 0
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice", "--dense", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dense"] == "used"
