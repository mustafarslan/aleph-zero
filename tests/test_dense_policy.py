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


def test_a_question_waits_for_a_waking_worker_only_where_nobody_set_it_a_time_limit(repo, fake_worker, monkeypatch):
    # A worker that slept (ten idle minutes) takes half a second to wake, and a question used to wait 0.3 s for its
    # vector whoever asked: the first question after every pause was answered by words alone, which is most of what
    # an agent asks. An agent's own call sets no limit and can take the half second; the plugin's question, with its
    # 400 ms, cannot. A worker that does not answer at all costs that wait once, not at every question.
    import time

    from aleph_zero import engine as engine_module
    from aleph_zero.embed.client import EmbedClient

    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    assert engine_module.QUERY_EMBED_SECONDS == 0.3 and engine_module.WAKING_EMBED_SECONDS == 0.8
    engine = Engine(repo.root, dense=True)
    engine.search("render_invoice")
    engine.embed()
    real = EmbedClient.embed

    def waking(seconds):
        def embed(self, texts, kind):
            time.sleep(seconds)                     # as a worker loading its model answers: late, and then rightly
            return real(self, texts, kind)
        return embed

    def asked(**options):
        started = time.monotonic()
        answer = engine.search("render_invoice", **options)
        return answer, time.monotonic() - started

    monkeypatch.setattr(EmbedClient, "embed", waking(0.5))
    answer, took = asked()
    assert answer.dense == "used" and 0.5 <= took < 1.0                    # nobody set a limit: the half second is waited for
    answer, took = asked(timeout_ms=400)
    assert "did not answer" in answer.dense and took < 0.45 and answer.hits[0].path == "src/billing/invoice.py"

    monkeypatch.setattr(EmbedClient, "embed", lambda self, texts, kind: real(self, ["HANG " + text for text in texts], kind))
    answer, took = asked()
    assert "did not answer" in answer.dense and took < 1.0 and answer.hits[0].path == "src/billing/invoice.py"
    answer, took = asked()
    assert "did not answer" in answer.dense and took < 0.6                 # it did not answer just now: no second long wait


def test_cpp_has_a_verdict_in_favour_and_says_on_what():
    """The same gate, written down for C++ before its run: 108 questions from five repositories, asked by their first
    line. The first run counted four questions twice (+8.1); on 108 distinct ones: +6.7 points, interval +1.4 to +12.3."""
    verdict = dense_policy.VERDICTS["cpp"]
    assert verdict.helps is True and "108" in verdict.evidence and "five repositories" in verdict.evidence and "ADR-002" in verdict.evidence
    assert "+6.7 points" in verdict.evidence and "+1.4 to +12.3" in verdict.evidence and "8.1" not in verdict.evidence
    on, why = dense_policy.decide(None, "cpp")
    assert on is True and "vectors help" in why
    assert dense_policy.decide(None, "c")[0] is False and "no benchmark has been run for c " in dense_policy.decide(None, "c")[1]      # C was not run: its own questions, its own verdict
    assert dense_policy.decide(False, "cpp") == (False, "switched off by hand")


def test_a_repository_s_own_choice_decides_where_no_flag_does_and_is_read_again_when_it_changes(repo, monkeypatch):
    """ADR-013: search by meaning can be chosen for one repository and kept, beside its index."""
    from aleph_zero import gitio, settings

    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    chosen = settings.repo_file(gitio.common_dir(repo.root))
    engine = Engine(repo.root)                                   # Python: the verdict is against
    assert engine.status()["vectors"]["enabled"] is False
    settings.change(chosen, dense=True)
    status = engine.status()["vectors"]                          # the same engine: a server that is running sees the choice
    assert status["enabled"] is True and "this repository" in status["why"] and "aleph setup" in status["why"]
    assert engine.search("render_invoice").dense != ""           # decided on: the answer says why no vector was used yet
    assert Engine(repo.root, dense=False).status()["vectors"]["enabled"] is False        # a flag for one process still wins
    settings.change(chosen, dense=False)
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(True, "test")})
    off = Engine(repo.root).status()["vectors"]                  # the verdict is for, the repository said no
    assert off["enabled"] is False and "this repository" in off["why"]
    settings.change(chosen, dense=None)
    assert engine.status()["vectors"]["enabled"] is True         # no choice any more: the verdict again
    settings.change(chosen, dense=False)
    assert engine.status()["vectors"]["enabled"] is False        # and the same engine sees each change, not what it kept


def test_go_typescript_javascript_and_java_each_have_their_own_verdict_from_their_own_questions():
    """The owner's release of 2026-10-13. The same gate for each: the first line, 5 points, an interval that excludes zero."""
    verdicts = dense_policy.VERDICTS
    assert verdicts["typescript"].helps is True and "+7.3 points" in verdicts["typescript"].evidence and "110 questions" in verdicts["typescript"].evidence
    for language, gain, questions in (("go", "+2.0", "111"), ("java", "+0.1", "296")):
        verdict = verdicts[language]
        assert verdict.helps is False and f"{gain} points" in verdict.evidence and f"{questions} questions" in verdict.evidence
        assert "holds zero" in verdict.evidence and "whole issue text" in verdict.evidence and "ADR-002" in verdict.evidence
        on, why = dense_policy.decide(None, language)
        assert on is False and "ALEPH_DENSE=1" in why            # and how to have them all the same
    assert dense_policy.decide(None, "typescript")[0] is True and dense_policy.decide(None, "tsx")[0] is True


def test_a_verdict_is_read_from_the_repositories_the_product_calls_that_language_and_javascript_is_on_by_it():
    """A verdict is applied by a repository's main language, so that is the population it is read from, not a question
    set's label (the deferred list, entry 1b-2). Read so, JavaScript passed on what had been asked; it was switched on
    only after questions nobody had asked passed alone, by a rule written before they were run."""
    javascript = dense_policy.VERDICTS["javascript"]
    assert javascript.helps is True and dense_policy.decide(None, "javascript")[0] is True
    assert "+16.2 points" in javascript.evidence and "164 questions" in javascript.evidence      # the questions never asked, alone
    assert "+9.8" in javascript.evidence and "448" in javascript.evidence                        # and all of them together
    assert "main language" in javascript.evidence and "main language" in dense_policy.VERDICTS["typescript"].evidence
    assert "main language" in dense_policy.VERDICTS["go"].evidence
    for unmeasured in ("c", "rust"):
        assert "no benchmark has been run" in dense_policy.decide(None, unmeasured)[1]


def test_the_query_s_vector_is_asked_for_while_git_is_asked_about_the_worktree(repo, fake_worker, monkeypatch):
    # The worker needs 17 to 45 ms for a question's vector and git 40 to 100 ms for the worktree; one after the other
    # was a fifth to a third of a question with vectors. The same vector, the same answer.
    from aleph_zero import gitio
    from aleph_zero.embed.client import EmbedClient

    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    engine = Engine(repo.root, dense=True)
    first = engine.search("how is an invoice rendered")          # the first question: nothing is known of the worktree yet
    engine.embed()
    expected = engine.search("how is an invoice rendered")
    assert expected.dense == "used"
    events: list[str] = []
    real_embed, real_status = EmbedClient.embed, gitio.status

    def embed(self, texts, kind):
        events.append("the worker is asked")
        return real_embed(self, texts, kind)

    def status(root):
        found = list(real_status(root))
        events.append("git has answered")
        return found

    monkeypatch.setattr(EmbedClient, "embed", embed)
    monkeypatch.setattr(gitio, "status", status)
    answer = engine.search("how is an invoice rendered")
    assert events.index("the worker is asked") < events.index("git has answered") and events.count("the worker is asked") == 1
    assert answer.dense == "used" and [(hit.path, hit.start_line, hit.score) for hit in answer.hits] == [(hit.path, hit.start_line, hit.score) for hit in expected.hits]
    assert first.hits                                             # and the first question was answered as ever


def test_a_question_s_early_vector_is_not_asked_for_where_vectors_take_no_part(repo, fake_worker, monkeypatch):
    from aleph_zero.embed.client import EmbedClient

    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    asked: list[str] = []
    real = EmbedClient.embed
    monkeypatch.setattr(EmbedClient, "embed", lambda self, texts, kind: asked.append(kind) or real(self, texts, kind))
    off = Engine(repo.root, dense=False)
    for _ in range(3):
        assert off.search("how is an invoice rendered").dense == ""
    undecided = Engine(repo.root)                                 # Python: the verdict is against, so nothing is asked either
    for _ in range(3):
        undecided.search("how is an invoice rendered")
    assert asked == []
