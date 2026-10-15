import json
import subprocess
import sys
from pathlib import Path

from aleph_zero import cli
from aleph_zero.view import build_view


def test_index_then_search_as_json(repo, capsys):
    assert cli.main(["--cwd", str(repo.root), "index"]) == 0
    assert "indexed 3 files" in capsys.readouterr().out
    assert cli.main(["--cwd", str(repo.root), "search", "refresh token", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending"] == 0
    assert payload["hits"][0]["path"] == "src/auth/token_store.py"


def test_search_prints_location_lines(repo, capsys):
    cli.main(["--cwd", str(repo.root), "search", "render_invoice", "-k", "1"])
    out = capsys.readouterr().out
    assert out.startswith("src/billing/invoice.py:1-3  function  render_invoice")


def test_status_reports_the_view(repo, capsys):
    cli.main(["--cwd", str(repo.root), "index"])
    capsys.readouterr()
    repo.write("new.py", "x = 1\n")
    assert cli.main(["--cwd", str(repo.root), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert (status["files_in_view"], status["dirty"], status["pending"]) == (4, 1, 1)


def test_a_directory_that_is_not_walked_is_an_error_message_not_a_traceback(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))   # outside a repository, a home directory is refused
    assert cli.main(["--cwd", str(tmp_path), "status"]) == 2
    said = capsys.readouterr()
    assert "aleph:" in said.err
    # /aleph configure reads what is built on this machine from the same command, refused or not.
    shown = json.loads(said.out)
    assert shown["repository"] is False and shown["refused"] and "worker_installed" in shown["vectors"]
    assert "aeon_installed" in shown["memory"]


def test_search_output_survives_a_non_ascii_path(repo, capsys):
    repo.write("docs/café.md", "# Menü\n\nespresso_machine settings\n")
    assert cli.main(["--cwd", str(repo.root), "search", "espresso_machine"]) == 0
    assert "docs/café.md:1-3  doc  Menü" in capsys.readouterr().out


def test_index_with_no_time_to_work_stops_instead_of_looping(repo):
    result = subprocess.run(
        [sys.executable, "-m", "aleph_zero.cli", "--cwd", str(repo.root), "index", "--budget", "0"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0 and "pending 3" in result.stdout


def test_index_catches_up_when_a_file_changes_while_it_runs(repo, monkeypatch, capsys):
    repo.write("src/live.py", "first = 1\n")
    stale = build_view(repo.root)
    repo.write("src/live.py", "second = 2\n")
    views = iter([stale])
    monkeypatch.setattr(cli, "build_view", lambda cwd: next(views, None) or build_view(cwd))
    assert cli.main(["--cwd", str(repo.root), "index"]) == 0
    assert "pending 0" in capsys.readouterr().out


def test_the_commands_a_person_runs_prepare_files_in_several_processes_and_a_session_does_not(repo, monkeypatch, capsys):
    # A cold index on several cores: Django 8.3 -> 3.5 s, vscode 12.4 -> 4.5 s. Not inside a session: its memory share.
    from aleph_zero import indexer, update
    from aleph_zero.engine import Engine

    asked: list[int] = []
    real = indexer.Index.sync
    monkeypatch.setattr(indexer.Index, "sync", lambda self, *a, **k: asked.append(self.workers) or real(self, *a, **k))
    cli.main(["--cwd", str(repo.root), "index"])
    assert asked and min(asked) == indexer.PREPARE_PROCESSES >= 1
    asked.clear()
    repo.write("src/new.py", "def fresh():\n    return 1\n")
    update.run(repo.root, fetch=False)
    assert asked and asked[0] == indexer.PREPARE_PROCESSES       # its indexing; the parts after it look again and find nothing
    asked.clear()
    Engine(repo.root).search("fresh")
    assert asked and set(asked) == {0}


def test_the_version_is_the_package_s(capsys):
    from importlib.metadata import version
    import pytest

    with pytest.raises(SystemExit) as done:
        cli.main(["--version"])
    assert done.value.code == 0 and capsys.readouterr().out.strip() == f"aleph-zero {version('aleph-zero')}"


def test_a_grammar_that_could_not_load_is_said_once_on_a_terminal(repo, capsys, monkeypatch):
    # Offline, or behind a proxy, the grammar download fails and search answers by words only: a person is told why
    # and what fetches it, instead of finding `symbol` and `outline` empty without a word.
    from aleph_zero import firstrun
    from aleph_zero.lang import parse

    def offline(name):
        raise RuntimeError("could not download the grammar: connection refused")
    monkeypatch.setattr(parse.pack, "get_parser", offline)
    monkeypatch.setattr(parse, "_PARSERS", {})
    monkeypatch.setattr(parse, "_FAILED", {})
    monkeypatch.setattr(firstrun, "hint_wanted", lambda: True)
    assert cli.main(["--cwd", str(repo.root), "search", "refresh token"]) == 0
    err = capsys.readouterr().err
    assert err.count("could not be loaded") == 1 and "python" in err and "--fetch-grammars" in err
    monkeypatch.setattr(firstrun, "hint_wanted", lambda: False)         # an agent's pipe: nothing
    assert cli.main(["--cwd", str(repo.root), "search", "refresh token"]) == 0
    assert "could not be loaded" not in capsys.readouterr().err


def test_started_as_aleph_zero_it_names_aleph_zero_in_what_it_tells_a_person_to_run(tmp_path):
    # TeX Live installs its own `aleph`: on such a machine a person runs `aleph-zero`, and `aleph setup` would run TeX.
    import os
    bin_dir = Path(sys.executable).parent
    started = {}
    for name in ("aleph-zero", "aleph"):
        if not (bin_dir / name).exists():
            import pytest
            pytest.skip("the entry points are not installed in this environment")
        env = {**os.environ, "XDG_CACHE_HOME": str(tmp_path / "cache")}
        started[name] = subprocess.run([str(bin_dir / name), "doctor"], cwd=tmp_path, capture_output=True, text=True, env=env).stdout
    assert "`aleph-zero embed setup`" in started["aleph-zero"] and "`aleph embed" not in started["aleph-zero"]
    assert "`aleph embed setup`" in started["aleph"]


def test_ctrl_c_says_one_line_and_keeps_what_was_done(repo, capsys, monkeypatch):
    def interrupted(args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "_index", interrupted)
    assert cli.main(["--cwd", str(repo.root), "index"]) == 130
    err = capsys.readouterr().err
    assert "interrupted" in err and err.count("\n") <= 2


def test_an_operating_system_error_is_one_line_not_a_traceback(repo, capsys, monkeypatch):
    def refused(args):
        raise PermissionError(13, "Permission denied", "/some/file")
    monkeypatch.setattr(cli, "_index", refused)
    assert cli.main(["--cwd", str(repo.root), "index"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("aleph: ") and "/some/file" in err and "Traceback" not in err


def test_a_reader_that_goes_away_is_not_an_error(repo, tmp_path):
    # `aleph search ... | head -1`: the pipe closes while results are still being written.
    done = subprocess.run(f"{sys.executable} -m aleph_zero.cli --cwd {repo.root} search refresh -k 50 --detail full | head -c 1",
                          shell=True, capture_output=True, text=True, env={"XDG_CACHE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert "BrokenPipeError" not in done.stderr and "Exception ignored" not in done.stderr


def test_cwd_is_taken_after_the_command_too(repo, capsys):
    assert cli.main(["search", "refresh token", "--cwd", str(repo.root)]) == 0
    assert "token_store.py" in capsys.readouterr().out


def test_empty_answers_say_so(repo, capsys):
    for argv, said in ((["symbol", "no_such_name_zz"], "no definition named"), (["memory", "list"], "no memories"),
                       (["scip", "list"], "no SCIP")):
        assert cli.main(["--cwd", str(repo.root), *argv]) == 0
        assert said in capsys.readouterr().out


def test_the_command_line_names_its_own_flags_when_results_are_left_out(empty_repo, capsys):
    for number in range(12):
        empty_repo.write(f"m{number}.py", f"def token_invoice_{number}(x):\n    return x\n")
    empty_repo.commit("many")
    assert cli.main(["--cwd", str(empty_repo.root), "search", "token invoice", "--budget-tokens", "40"]) == 0
    out = capsys.readouterr().out
    assert "more results not shown" in out and "--glob" in out and "path_glob" not in out
