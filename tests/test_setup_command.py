"""`aleph setup` (ADR-013): the checklist shown, asked on a terminal, and each answer kept."""
import json
import subprocess

import pytest

from aleph_zero import cli, dense_policy, firstrun, gitio, settings
from aleph_zero.dense_policy import Verdict
from aleph_zero.embed import setup as embed_setup
from aleph_zero.embed import worker
from aleph_zero.engine import Engine


@pytest.fixture
def docs_repo(tmp_path, make_repo):
    docs = make_repo(tmp_path / "handbook")
    docs.write("guides/mooring.md", "# Mooring\n\nThe zeppelin_mooring procedure takes two people.\n")
    docs.commit("docs")
    return docs


def _terminal(monkeypatch, *replies):
    """A person at a terminal who gives these answers, in order, and is asked nothing else."""
    left, asked = list(replies), []

    def reply(prompt=""):
        asked.append(prompt)
        assert left, f"asked for more than was expected: {prompt!r}"
        return left.pop(0)

    monkeypatch.setattr("builtins.input", reply)
    monkeypatch.setattr(firstrun, "on_terminal", lambda: True)
    return asked, left


def _setup(repo, *flags):
    return cli.main(["--cwd", str(repo.root), "setup", *flags])


def _states(repo, capsys):
    capsys.readouterr()
    assert _setup(repo, "--json") == 0
    return {step["key"]: step["state"] for step in json.loads(capsys.readouterr().out)["steps"]}


def test_without_a_terminal_the_list_is_shown_and_nothing_is_asked_or_kept(repo, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked without a terminal"))
    assert _setup(repo) == 0
    out = capsys.readouterr().out
    for title in ("Connect Claude Code", "embedding worker", "another repository", "Search by meaning", "untracked cache"):
        assert title in out
    assert out.count("open") >= 5 and "310 MB" in out and "aleph setup" in out and "--source" in out      # and how to answer it
    assert not settings.repo_file(gitio.common_dir(repo.root)).exists() and not settings.machine_file().exists()


def test_a_person_answers_once_and_is_not_asked_again(repo, docs_repo, capsys, monkeypatch):
    asked, left = _terminal(monkeypatch, "y",                      # Claude Code is connected
                            "n",                                   # no worker
                            str(docs_repo.root), "",               # one source, then none
                            "y")                                   # git's untracked cache; vectors are not asked: no worker
    assert _setup(repo) == 0
    assert left == [] and len(asked) == 5
    out = capsys.readouterr().out
    assert "claude" in out and "handbook" in out
    assert settings.read(settings.machine_file())["setup"] == {"claude": "done", "worker": "declined"}
    assert gitio.untracked_cache_enabled(repo.root)
    configured = subprocess.run(["git", "-C", str(repo.root), "config", "--get", "core.untrackedCache"], capture_output=True)
    assert configured.stdout.strip() == b""                        # no configuration was changed
    assert [hit.path for hit in Engine(repo.root).search("zeppelin_mooring").hits] == ["handbook:guides/mooring.md"]
    assert _states(repo, capsys) == {"claude": "done", "worker": "declined", "sources": "done", "vectors": "declined", "untracked_cache": "done"}
    _terminal(monkeypatch)                                         # again: nothing is open, so nothing is asked
    assert _setup(repo) == 0
    asked, left = _terminal(monkeypatch, "n")                      # asked for again: what was declined is a question again
    assert _setup(repo, "--again") == 0
    assert len(asked) == 1 and "worker" in asked[0] and left == []


def test_a_no_is_kept_and_an_interrupted_menu_leaves_the_rest_open(repo, capsys, monkeypatch):
    def interrupted(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(firstrun, "on_terminal", lambda: True)
    monkeypatch.setattr("builtins.input", interrupted)
    assert _setup(repo) == 0                                       # leaving is not an error
    assert "aleph setup" in capsys.readouterr().out
    assert set(_states(repo, capsys).values()) == {"open"}
    asked, left = _terminal(monkeypatch, "n", "n", "", "n")        # no to everything: an empty line for sources is a no
    assert _setup(repo) == 0
    assert left == [] and set(_states(repo, capsys).values()) == {"declined"}


def test_a_yes_to_search_by_meaning_makes_the_vectors_and_keeps_the_choice_for_the_repository(repo, fake_worker, capsys, monkeypatch):
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    asked, left = _terminal(monkeypatch, "n", "", "y", "n")        # the worker is there: Claude Code, sources, vectors, git's cache
    assert _setup(repo) == 0
    assert left == [] and "meaning" in asked[2] and "[y/N]" in asked[2]          # Python: the verdict is against, so no is the default
    assert settings.read(settings.repo_file(gitio.common_dir(repo.root)))["dense"] is True
    status = Engine(repo.root).status()["vectors"]
    assert status["enabled"] is True and status["with_vector"] == status["chunks"] > 0
    assert _states(repo, capsys)["vectors"] == "done"
    assert Engine(repo.root).search("render_invoice").dense == "used"


def test_where_the_verdict_is_for_vectors_yes_is_the_default_and_a_no_is_the_repository_s_choice(repo, capsys, monkeypatch):
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(True, "test")})
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    asked, left = _terminal(monkeypatch, "n", "", "n", "n")
    assert _setup(repo) == 0
    assert "[Y/n]" in asked[2]
    assert settings.read(settings.repo_file(gitio.common_dir(repo.root)))["dense"] is False
    assert Engine(repo.root).status()["vectors"]["enabled"] is False


def test_each_thing_has_a_flag_of_its_own_for_a_script_or_an_agent(repo, docs_repo, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although flags were given"))
    built = []
    monkeypatch.setattr(embed_setup, "install_binary", lambda source=None, report=None: built.append("binary") or "worker")
    monkeypatch.setattr(embed_setup, "install_model", lambda local=None, report=None: built.append("model") or "model")
    assert _setup(repo, "--source", str(docs_repo.root), "--untracked-cache", "--no-dense") == 0
    assert built == []                                             # nothing that costs was done without its own flag
    states = _states(repo, capsys)
    assert states["sources"] == "done" and states["untracked_cache"] == "done" and states["vectors"] == "declined"
    assert _setup(repo, "--worker") == 0
    assert built == ["binary", "model"]
    assert _setup(repo, "--dense") == 0
    assert settings.read(settings.repo_file(gitio.common_dir(repo.root)))["dense"] is True


def test_the_doctor_and_the_agent_s_status_show_the_same_list_and_act_on_nothing(repo, monkeypatch):
    from aleph_zero import doctor

    row = next(row for row in doctor.check(repo.root) if row.name == "setup")
    assert row.level == "note" and "aleph setup" in row.detail and "5 of 5" in row.detail
    status = Engine(repo.root).status()["setup"]
    assert status["open"] == ["claude", "worker", "sources", "vectors", "untracked_cache"] and "aleph setup" in status["how"]
    assert not settings.repo_file(gitio.common_dir(repo.root)).exists()         # looking keeps nothing
    _terminal(monkeypatch, "n", "n", "", "n")
    assert _setup(repo) == 0
    assert Engine(repo.root).status()["setup"]["open"] == []
    row = next(row for row in doctor.check(repo.root) if row.name == "setup")
    assert row.level == "ok" and "nothing is open" in row.detail


def test_the_hint_is_one_line_once_on_a_terminal_and_never_from_the_server(repo, capsys, monkeypatch):
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice"]) == 0
    assert "aleph setup" not in capsys.readouterr().err            # not a terminal: an agent or a pipe reads this
    monkeypatch.setattr(firstrun, "hint_wanted", lambda: True)     # standard error is a terminal
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice"]) == 0
    captured = capsys.readouterr()
    assert captured.err.count("aleph setup") == 1 and captured.err.count("\n") == 1 and "render_invoice" in captured.out
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice"]) == 0
    assert capsys.readouterr().err == ""                           # once
    assert "serve" not in cli.HINTED and "setup" not in cli.HINTED and "search" in cli.HINTED


def test_no_hint_where_setup_was_already_answered(repo, capsys, monkeypatch):
    _terminal(monkeypatch, "n", "n", "", "n")
    assert _setup(repo) == 0
    capsys.readouterr()
    monkeypatch.setattr(firstrun, "hint_wanted", lambda: True)
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice"]) == 0
    assert capsys.readouterr().err == ""
