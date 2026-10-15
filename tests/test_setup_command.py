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
    assert "aleph update" in out and "--print-hook" in out         # and how it stays up to date afterwards
    assert settings.read(settings.machine_file())["setup"] == {"claude": "done", "worker": "not-needed"}   # no where no was suggested
    assert gitio.untracked_cache_enabled(repo.root)
    configured = subprocess.run(["git", "-C", str(repo.root), "config", "--get", "core.untrackedCache"], capture_output=True)
    assert configured.stdout.strip() == b""                        # no configuration was changed
    assert [hit.path for hit in Engine(repo.root).search("zeppelin_mooring").hits] == ["handbook:guides/mooring.md"]
    assert _states(repo, capsys) == {"claude": "done", "worker": "declined", "memory": "declined", "sources": "done", "vectors": "declined", "untracked_cache": "done"}
    _terminal(monkeypatch)                                         # again: nothing is open, so nothing is asked
    assert _setup(repo) == 0
    asked, left = _terminal(monkeypatch, "y", "n")                 # asked for again: the line done on their word, and what was declined
    assert _setup(repo, "--again") == 0
    assert len(asked) == 2 and "Claude Code" in asked[0] and "worker" in asked[1] and left == []


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
    assert "dense" not in settings.read(settings.repo_file(gitio.common_dir(repo.root)))      # nothing chosen against a later verdict


def test_a_yes_to_search_by_meaning_makes_the_vectors_and_keeps_the_choice_for_the_repository(repo, fake_worker, capsys, monkeypatch):
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    asked, left = _terminal(monkeypatch, "n", "n", "", "y", "n")   # the worker is there: Claude Code, memory, sources, vectors, git's cache
    assert _setup(repo) == 0
    assert left == [] and "meaning" in asked[3] and "[y/N]" in asked[3]          # Python: the verdict is against, so no is the default
    assert settings.read(settings.repo_file(gitio.common_dir(repo.root)))["dense"] is True
    status = Engine(repo.root).status()["vectors"]
    assert status["enabled"] is True and status["with_vector"] == status["chunks"] > 0
    assert _states(repo, capsys)["vectors"] == "done"
    assert Engine(repo.root).search("render_invoice").dense == "used"


def test_where_the_verdict_is_for_vectors_yes_is_the_default_and_a_no_is_the_repository_s_choice(repo, capsys, monkeypatch):
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(True, "test")})
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    asked, left = _terminal(monkeypatch, "n", "n", "", "n", "n")   # Claude Code, memory, sources, vectors, git's cache
    assert _setup(repo) == 0
    assert "[Y/n]" in asked[3]
    assert settings.read(settings.repo_file(gitio.common_dir(repo.root)))["dense"] is False
    assert Engine(repo.root).status()["vectors"]["enabled"] is False


def test_each_thing_has_a_flag_of_its_own_for_a_script_or_an_agent(repo, docs_repo, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("asked although flags were given"))
    monkeypatch.setattr(firstrun, "on_terminal", lambda: True)     # even on a terminal: a flag is an answer, not a question
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
    assert row.level == "note" and "aleph setup" in row.detail and "6 of 6" in row.detail
    status = Engine(repo.root).status()["setup"]
    assert status["open"] == ["claude", "worker", "memory", "sources", "vectors", "untracked_cache"] and "aleph setup" in status["how"]
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


# ---- from the review of the branch (2026-10-13) ---------------------------------------------------


def test_the_status_answers_whatever_the_setup_list_runs_into_and_leaves_nothing_behind(repo, tmp_path, monkeypatch):
    cache = tmp_path / "fresh-cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    assert Engine(repo.root).status()["setup"]["open"]
    assert not (cache / "aleph-zero" / "worker").exists()          # looking does not make the worker's directory

    def unreadable():
        raise PermissionError("the cache directory cannot be written")

    monkeypatch.setattr(embed_setup, "installed", unreadable)
    status = Engine(repo.root).status()                            # a search works there; so must this
    assert status["setup"]["open"] == [] and "cannot be written" in status["setup"]["unknown"]


def test_a_step_that_fails_says_so_and_the_menu_goes_on(repo, capsys, monkeypatch):
    def no_compiler(source=None, report=None):
        raise embed_setup.SetupError("cmake is not installed")

    monkeypatch.setattr(embed_setup, "install_binary", no_compiler)
    asked, left = _terminal(monkeypatch, "n", "y", "", "y")        # the worker fails to build; the questions after it are still asked
    assert _setup(repo) == 0
    out = capsys.readouterr().out
    assert left == [] and "cmake is not installed" in out and "not done" in out
    states = _states(repo, capsys)
    assert states["worker"] == "open" and states["untracked_cache"] == "done"       # asked again next time; what worked is kept


def test_a_flag_that_fails_does_not_stop_the_others_and_the_command_says_it_failed(repo, capsys, monkeypatch):
    def no_compiler(source=None, report=None):
        raise embed_setup.SetupError("cmake is not installed")

    monkeypatch.setattr(embed_setup, "install_binary", no_compiler)
    assert _setup(repo, "--worker", "--untracked-cache") == 2
    assert "cmake is not installed" in capsys.readouterr().err
    assert _states(repo, capsys)["untracked_cache"] == "done"


def test_json_is_json_also_when_a_flag_did_something_and_no_hint_follows_a_setup_by_flag(repo, capsys, monkeypatch):
    assert _setup(repo, "--json", "--untracked-cache") == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["steps"] and "untracked cache" in captured.err      # what was done is said beside the data
    monkeypatch.setattr(firstrun, "hint_wanted", lambda: True)
    assert cli.main(["--cwd", str(repo.root), "search", "render_invoice"]) == 0
    assert capsys.readouterr().err == ""


def test_embed_without_the_worker_does_nothing_and_says_what_to_do_first(repo, capsys):
    assert _setup(repo, "--embed") == 2
    assert "aleph setup --worker" in capsys.readouterr().err
    assert "dense" not in settings.read(settings.repo_file(gitio.common_dir(repo.root)))
    with pytest.raises(SystemExit):
        _setup(repo, "--embed", "--no-dense")                      # make the vectors and do not use them: not a thing to ask for


def test_embed_makes_the_vectors_and_keeps_the_choice(repo, fake_worker, capsys, monkeypatch):
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    assert _setup(repo, "--embed") == 0
    assert settings.read(settings.repo_file(gitio.common_dir(repo.root)))["dense"] is True
    assert _states(repo, capsys)["vectors"] == "done"


def test_a_directory_that_is_not_a_repository_is_told_what_cannot_be_done_there(tmp_path, capsys):
    from aleph_zero import doctor

    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "todo.py").write_text("def later():\n    return 1\n")
    assert cli.main(["--cwd", str(plain), "setup"]) == 0
    assert "another repository" not in capsys.readouterr().out     # not offered: sources are linked to a repository
    assert cli.main(["--cwd", str(plain), "setup", "--source", str(tmp_path), "--untracked-cache"]) == 2
    err = capsys.readouterr().err
    assert "not a git repository" in err and "rev-parse" not in err and err.count("\n") <= 3
    assert any(row.name == "setup" for row in doctor.check(plain))


def test_a_no_to_vectors_where_the_verdict_is_against_is_an_answer_and_not_a_choice(repo, capsys, monkeypatch):
    monkeypatch.setattr(embed_setup, "installed", lambda: True)    # Python: the verdict is against, the worker is there
    _terminal(monkeypatch, "n", "n", "", "", "n")                  # memory: no; a bare return at the vectors question
    assert _setup(repo) == 0
    kept = settings.read(settings.repo_file(gitio.common_dir(repo.root)))
    assert "dense" not in kept and kept["setup"]["vectors"] == "declined"       # a later verdict in favour is not outranked
    assert _states(repo, capsys)["vectors"] == "declined"


# ---- before the owner tries it (2026-10-13) --------------------------------------------------------


def test_the_command_for_claude_code_names_an_engine_that_can_be_started(repo, capsys, monkeypatch):
    import sys
    from pathlib import Path

    # Under `uv run` the checkout's `aleph` is on PATH, and in Claude Code's own environment it is not: the name
    # alone would not start there. Where this engine is, is always right.
    here = Path(sys.executable).with_name("aleph-zero")         # never `aleph`, which TeX Live also installs
    for found in (lambda name: None, lambda name: f"/somewhere/else/{name}"):
        monkeypatch.setattr("shutil.which", found)
        assert _setup(repo) == 0
        out = capsys.readouterr().out
        assert f"claude mcp add --scope user aleph-zero -- {here} serve" in out and "-- aleph serve" not in out


def test_the_owner_s_example_a_documentation_repository_is_linked_embedded_and_found_by_meaning(repo, docs_repo, fake_worker, capsys, monkeypatch):
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    _terminal(monkeypatch, "n", "n", str(docs_repo.root), "", "y", "n")  # Claude Code, memory, the source, no more, vectors, git's cache
    assert _setup(repo) == 0
    engine = Engine(repo.root)
    status = engine.status()
    assert status["vectors"]["enabled"] is True and status["vectors"]["with_vector"] == status["vectors"]["chunks"]
    assert len(status["sources"]) == 1 and status["vectors"]["chunks"] == status["chunks_in_view"]      # the linked chunks are among them
    answer = engine.search("the procedure for mooring a zeppelin")
    assert answer.dense == "used" and "handbook:guides/mooring.md" in [hit.path for hit in answer.hits]


def test_again_also_asks_about_a_line_that_is_done_only_on_the_person_s_word(repo, capsys, monkeypatch):
    _terminal(monkeypatch, "y", "n", "", "n")                      # Claude Code: connected, they said
    assert _setup(repo) == 0
    asked, left = _terminal(monkeypatch, "n", "n", "", "n")        # asked again: that line too, since nothing checks it
    assert _setup(repo, "--again") == 0
    assert left == [] and "Claude Code" in asked[0]
    assert _states(repo, capsys)["claude"] == "declined"


def test_the_vectors_line_says_what_is_known_of_whole_questions_where_the_verdict_is_against(repo, capsys):
    assert _setup(repo) == 0
    out = capsys.readouterr().out
    assert "whole" in out and "every language measured" in out     # the default reads first lines; a whole question is another matter


def test_the_worker_is_installed_from_the_home_directory_where_the_readme_s_new_terminal_starts(tmp_path, capsys, monkeypatch):
    # README "Once per machine": `aleph setup --worker` in a new terminal, before any `cd` into a project. The worker is
    # the machine's, so the directory it is run from does not matter, and the home directory must not stop it.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    built = []
    monkeypatch.setattr(embed_setup, "install_binary", lambda source=None, report=None: built.append("binary") or "worker")
    monkeypatch.setattr(embed_setup, "install_model", lambda local=None, report=None: built.append("model") or "model")
    assert cli.main(["--cwd", str(home), "setup", "--worker"]) == 0
    out, err = capsys.readouterr()
    assert built == ["binary", "model"] and "installed worker and model" in out
    assert "does not walk" not in err and "Traceback" not in err
    assert "aleph" in out and "project" in out                     # where the rest of the list is shown
    assert settings.read(settings.machine_file()).get("setup", {}).get("worker") is None    # a yes: nothing declined
    assert cli.main(["--cwd", str(home), "setup", "--worker", "--untracked-cache"]) == 2   # a project's flag still needs one


def test_a_no_to_the_worker_where_nothing_needed_it_is_asked_again_where_vectors_help(repo, tmp_path, make_repo, capsys, monkeypatch):
    # The worker is the machine's. Declined with Enter at "[y/N]" in a Python repository, where the verdict is
    # against vectors, it must not silence the question in a repository whose verdict is for them.
    _terminal(monkeypatch, "y", "", "", "n")                       # Claude Code; worker: Enter (no); sources; git's cache
    assert _setup(repo) == 0
    assert _states(repo, capsys)["worker"] == "declined"
    other = make_repo(tmp_path / "web")
    other.write("src/app.py", "def handler(request):\n    return request\n")
    other.commit("app")
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(True, "test")})      # vectors help here
    states = _states(other, capsys)
    assert states["worker"] == "open" and states["vectors"] != "declined"
    asked, left = _terminal(monkeypatch, "n", "", "n")             # asked again; a no where yes was suggested is final
    assert cli.main(["--cwd", str(other.root), "setup"]) == 0
    assert "embedding worker" in asked[0] and "[Y/n]" in asked[0] and left == []
    assert _states(other, capsys)["worker"] == "declined"


def test_with_the_plugin_installed_claude_code_is_connected_and_no_second_server_is_suggested(repo, tmp_path, capsys, monkeypatch):
    # `claude mcp add` beside the plugin would start a second engine for every session.
    plugins = tmp_path / "claude" / "plugins"
    plugins.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert _states(repo, capsys)["claude"] == "open"
    assert _setup(repo) == 0
    assert "claude plugin install aleph-zero@aleph-zero" in capsys.readouterr().out    # the plugin is offered beside it
    (plugins / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {"aleph-zero@aleph-zero": [{"scope": "user"}]}}))
    assert _states(repo, capsys)["claude"] == "done"
    assert _setup(repo) == 0
    out = capsys.readouterr().out
    assert "plugin" in out and "claude mcp add" not in out


def test_a_no_to_git_s_untracked_cache_is_not_answered_with_advice_to_change_git_s_configuration(repo, capsys, monkeypatch):
    _terminal(monkeypatch, "y", "n", "", "n")
    assert _setup(repo) == 0
    assert "git config --global" not in capsys.readouterr().out


def test_the_worker_alone_does_not_index_a_directory_that_is_not_a_project(tmp_path, capsys, monkeypatch):
    # `aleph setup --worker` in ~/Downloads must not walk thousands of files into the cache.
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "notes.py").write_text("def later():\n    return 1\n")
    monkeypatch.setattr(embed_setup, "install_binary", lambda source=None, report=None: "worker")
    monkeypatch.setattr(embed_setup, "install_model", lambda local=None, report=None: "model")
    assert cli.main(["--cwd", str(downloads), "setup", "--worker"]) == 0
    assert "installed worker and model" in capsys.readouterr().out
    assert not list((settings.machine_file().parent / "index").glob("*"))      # nothing indexed for it


def test_a_yes_in_the_menu_says_what_it_installed(repo, capsys, monkeypatch):
    monkeypatch.setattr(embed_setup, "install_binary", lambda source=None, report=None: "the-worker")
    monkeypatch.setattr(embed_setup, "install_model", lambda local=None, report=None: "the-model")
    _terminal(monkeypatch, "y", "y", "", "y")                      # Claude Code, worker: yes, no sources, git's cache: yes
    assert _setup(repo) == 0
    out = capsys.readouterr().out
    assert "installed the-worker and the-model" in out and "untracked cache is on" in out


def test_memory_by_meaning_is_on_the_list_after_the_worker_and_suggested_where_the_worker_is(repo, capsys, monkeypatch):
    from aleph_zero import aeon_setup

    states = _states(repo, capsys)
    assert list(states)[:3] == ["claude", "worker", "memory"] and states["memory"] == "open"
    built = []
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    monkeypatch.setattr(aeon_setup, "install", lambda source=None, report=None: built.append(source) or "lib")
    asked, left = _terminal(monkeypatch, "y", "", "", "", "n")     # Claude Code; memory: Enter (yes, suggested); sources; vectors; cache
    assert _setup(repo) == 0
    assert any("Memory by meaning" in question or "Aeon" in question for question in asked) and built == [None]
    assert "[Y/n]" in next(question for question in asked if "Aeon" in question)


def test_search_by_meaning_is_switched_in_one_line_without_indexing_and_can_go_back_to_the_verdict(repo, capsys, monkeypatch):
    # For /aleph configure: a switch, not a 30-second pass over the repository and a 15-line list.
    from aleph_zero import cli as cli_module

    monkeypatch.setattr(firstrun, "on_terminal", lambda: False)
    monkeypatch.setattr(cli_module, "_open", lambda cwd: pytest.fail("indexed for a switch"))
    for flag, chosen in (("--dense", True), ("--no-dense", False), ("--auto-dense", None)):
        assert _setup(repo, flag) == 0
        out = capsys.readouterr().out.strip()
        assert out.count("\n") == 0 and "search by meaning" in out
        assert settings.read(settings.repo_file(gitio.common_dir(repo.root))).get("dense") is chosen


def test_the_status_says_the_verdict_the_choice_and_whether_the_worker_is_there(repo):
    Engine(repo.root).search("refresh token")                           # indexed: its main language is known
    vectors = Engine(repo.root).status()["vectors"]
    assert vectors["chosen"] is None and vectors["verdict"]["helps"] is False and "python" in vectors["verdict"]["why"]
    assert vectors["worker_installed"] is False
    settings.change(settings.repo_file(gitio.common_dir(repo.root)), dense=True)
    vectors = Engine(repo.root).status()["vectors"]
    assert vectors["chosen"] is True and "python" in vectors["verdict"]["why"]      # the verdict is still said
