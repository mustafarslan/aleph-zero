from pathlib import Path
import json

import pytest

from aleph_zero import cli, dense_policy
from aleph_zero.dense_policy import Verdict
from aleph_zero.embed import setup as embed_setup
from aleph_zero.embed import worker
from aleph_zero.engine import Engine


def test_the_main_language_of_a_worktree_is_the_one_with_the_most_source(repo):
    repo.write("web/app.ts", "export function start(): number { return 1; }\n")
    engine = Engine(repo.root)
    engine.search("anything")
    status = engine.status()
    assert status["main_language"] == "python" and 0.5 < status["language_share"]["python"] < 1.0
    assert set(status["language_share"]) == {"python", "typescript"}   # documentation and data files are not languages
    for i in range(12):
        repo.write(f"web/module_{i}.ts", f"export function handler{i}(input: string): string {{ return input + '{i}'; }}\n" * 6)
    engine.search("anything")
    assert engine.status()["main_language"] == "typescript"


def test_a_worktree_with_no_source_code_has_no_main_language(empty_repo):
    empty_repo.write("README.md", "# Notes\n\nOnly prose here.\n")
    engine = Engine(empty_repo.root)
    engine.search("notes")
    status = engine.status()
    assert status["main_language"] is None and status["language_share"] == {}


def test_the_decision_follows_the_verdict_for_the_main_language(monkeypatch):
    monkeypatch.setattr(dense_policy, "VERDICTS", {
        "python": Verdict(True, "+6 points on 400 questions"), "go": Verdict(False, "+1 point on 380 questions")})
    assert dense_policy.decide(None, "python") == (True, "the benchmark for python says vectors help (+6 points on 400 questions)")
    assert dense_policy.decide(None, "go") == (False, "the benchmark for go says vectors do not help enough (+1 point on 380 questions); "
                                                       "`--dense` or ALEPH_DENSE=1 switches them on by hand")
    off, why = dense_policy.decide(None, "rust")
    assert off is False and "no benchmark has been run for rust" in why
    assert dense_policy.decide(None, None)[0] is False
    assert dense_policy.decide(True, "rust") == (True, "switched on by hand")      # the owner's choice always wins
    assert dense_policy.decide(False, "python") == (False, "switched off by hand")
    assert dense_policy.decide(None, "tsx")[1] == dense_policy.decide(None, "typescript")[1]   # one verdict for both


def test_nothing_is_on_by_default_until_a_benchmark_says_so():
    assert all(isinstance(verdict, Verdict) for verdict in dense_policy.VERDICTS.values())
    for language in ("python", "typescript", "javascript", "go", "rust", "java", "cpp", "c"):
        enabled, why = dense_policy.decide(None, language)
        assert enabled is (language in dense_policy.VERDICTS and dense_policy.VERDICTS[language].helps), why


def test_an_engine_left_to_decide_uses_vectors_only_where_the_verdict_allows(repo, fake_worker, monkeypatch):
    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(True, "test")})
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    engine = Engine(repo.root)                      # no setting: decide per worktree
    engine.search("render_invoice")
    engine.embed()
    answer = engine.search("render_invoice")
    assert answer.dense == "used"
    status = engine.status()["vectors"]
    assert status["enabled"] is True and "python" in status["why"]
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(False, "test")})
    assert Engine(repo.root).search("render_invoice").dense == ""
    assert Engine(repo.root).status()["vectors"]["enabled"] is False
    assert Engine(repo.root, dense=True).search("render_invoice").dense == "used"


def test_command_line_settings(repo, monkeypatch, capsys):
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(True, "test")})
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    monkeypatch.setattr(worker, "available", lambda wait=2.0: None)
    monkeypatch.setattr(worker, "socket_path", lambda: Path("/tmp/az-no-such-worker.sock"))
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice", "--no-dense", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dense"] == ""
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dense"] != ""      # decided on: the answer says why vectors were not used
    monkeypatch.setenv("ALEPH_DENSE", "0")
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["dense"] == ""
    with pytest.raises(SystemExit):
        cli.main(["--cwd", str(repo.root), "search", "x", "--dense", "--no-dense"])


def test_python_has_its_verdict_and_it_is_backed_by_numbers():
    """The gate was fixed beforehand: 5 points of file recall@10 on issue titles, with an interval that excludes zero.
    The 165 questions held out gave +1.6, and all 400 together +3.9: vectors help Python, by less than the bar."""
    verdict = dense_policy.VERDICTS["python"]
    assert verdict.helps is False and "400" in verdict.evidence and "165" in verdict.evidence and "ADR-002" in verdict.evidence
    on, why = dense_policy.decide(None, "python")
    assert on is False and "do not help enough" in why and "ALEPH_DENSE=1" in why        # and how to have them all the same
    assert dense_policy.decide(True, "python") == (True, "switched on by hand")


def test_left_to_decide_and_with_no_worker_installed_nothing_is_said_about_vectors(repo, monkeypatch):
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(True, "test")})
    engine = Engine(repo.root)   # the verdict says on, but `aleph embed setup` was never run on this machine
    answer = engine.search("render_invoice")
    assert answer.dense == "" and "semantic" not in engine.render(answer)
    status = engine.status()["vectors"]
    assert status["enabled"] is False and "aleph embed setup" in status["why"]
    asked = Engine(repo.root, dense=True).search("render_invoice")   # asked for by hand: the answer says why not
    assert asked.dense != ""


def test_a_worker_that_is_waking_does_not_hold_a_query_up(repo, fake_worker, monkeypatch):
    import time

    from aleph_zero.embed.client import EmbedClient

    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    engine = Engine(repo.root, dense=True)
    engine.search("render_invoice")
    engine.embed()
    real = EmbedClient.embed
    monkeypatch.setattr(EmbedClient, "embed", lambda self, texts, kind: real(self, ["HANG " + text for text in texts], kind))
    started = time.monotonic()
    answer = engine.search("render_invoice")
    assert time.monotonic() - started < 1.0
    assert answer.hits[0].path == "src/billing/invoice.py" and "did not answer" in answer.dense
