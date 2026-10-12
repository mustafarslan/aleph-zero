import json
import subprocess
import sys

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
    monkeypatch.setenv("HOME", str(tmp_path))       # outside a repository, a home directory is refused
    assert cli.main(["--cwd", str(tmp_path), "status"]) == 2
    assert "aleph:" in capsys.readouterr().err


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
