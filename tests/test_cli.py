import json

from aleph_zero import cli


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
    assert out.startswith("src/billing/invoice.py:1-3  block")


def test_status_reports_the_view(repo, capsys):
    cli.main(["--cwd", str(repo.root), "index"])
    capsys.readouterr()
    repo.write("new.py", "x = 1\n")
    assert cli.main(["--cwd", str(repo.root), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert (status["files_in_view"], status["dirty"], status["pending"]) == (4, 1, 1)


def test_outside_a_repository_is_an_error_message_not_a_traceback(tmp_path, capsys):
    assert cli.main(["--cwd", str(tmp_path), "status"]) == 2
    assert "aleph:" in capsys.readouterr().err


def test_search_output_survives_a_non_ascii_path(repo, capsys):
    repo.write("docs/café.md", "# Menü\n\nespresso_machine settings\n")
    assert cli.main(["--cwd", str(repo.root), "search", "espresso_machine"]) == 0
    assert "docs/café.md:1-3  doc  Menü" in capsys.readouterr().out
