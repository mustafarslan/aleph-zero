"""`aleph update` (ADR-014): what changed is brought up to date in one go, for a person, a hook or a schedule."""
import json
import subprocess
import threading

import pytest

from aleph_zero import cli, dense_policy, firstrun, gitio, settings, sources, update
from aleph_zero.dense_policy import Verdict
from aleph_zero.embed import setup as embed_setup
from aleph_zero.embed import worker
from aleph_zero.engine import Engine


def _update(repo, *flags):
    return cli.main(["--cwd", str(repo.root), "update", *flags])


def _report(repo, capsys, *flags):
    capsys.readouterr()
    code = _update(repo, "--json", *flags)
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def with_worker(fake_worker, monkeypatch):
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    return fake_worker


def test_what_a_commit_changed_is_indexed_and_a_second_run_says_nothing_changed(repo, capsys):
    assert _update(repo) == 0
    first = capsys.readouterr().out
    assert "indexed" in first and "files" in first
    repo.write("src/billing/tax.py", "def vat_rate(country):\n    return 0.2\n")
    repo.commit("a merged pull request")
    code, report = _report(repo, capsys)
    assert code == 0 and report["indexed"] == 1 and report["pending"] == 0 and report["failed"] == []
    assert update.text(report)[0] == "indexed 1 file"              # said as a person would say it
    assert _update(repo) == 0
    assert capsys.readouterr().out.strip() == "nothing changed: the index matches the worktree"
    assert Engine(repo.root).status()["pending"] == 0


def test_vectors_are_made_where_search_by_meaning_is_on_and_never_switched_on_by_this(repo, with_worker, capsys, monkeypatch):
    asked = []
    real = worker.ensure
    monkeypatch.setattr(worker, "ensure", lambda: asked.append(1) or real())
    code, report = _report(repo, capsys)                           # Python: the verdict is against, nobody chose otherwise
    assert code == 0 and report["vectors"]["made"] == 0 and "do not help enough" in report["vectors"]["why"] and asked == []
    assert "dense" not in settings.read(settings.repo_file(gitio.common_dir(repo.root)))
    settings.change(settings.repo_file(gitio.common_dir(repo.root)), dense=True)       # the owner's choice in `aleph setup`
    code, report = _report(repo, capsys)
    assert code == 0 and report["vectors"]["made"] > 0 and report["vectors"]["left"] == 0
    status = Engine(repo.root).status()["vectors"]
    assert status["with_vector"] == status["chunks"]
    repo.write("src/billing/tax.py", "def vat_rate(country):\n    return 0.2\n")
    repo.commit("a merged pull request")
    code, report = _report(repo, capsys)
    assert report["vectors"]["made"] == 1                          # the one new chunk, not the repository again


def test_a_source_linked_by_url_is_fetched_and_its_new_content_found_unless_told_not_to(repo, make_repo, tmp_path, capsys):
    docs = make_repo(tmp_path / "team-docs")
    docs.write("guides/auth.md", "# Authentication\n\nTokens are rotated by the wombat_rotator service.\n")
    docs.commit("docs")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(docs.root), str(remote)], check=True)
    sources.add(repo.root, remote.as_uri(), name="handbook")
    docs.write("guides/new.md", "# New\n\nThe quokka_scheduler is new.\n")
    docs.commit("more docs")
    docs.git("push", "-q", str(remote), "HEAD:refs/heads/main")
    code, report = _report(repo, capsys, "--no-fetch")
    assert code == 0 and report["sources"] == {} and Engine(repo.root).search("quokka_scheduler").hits == []
    code, report = _report(repo, capsys)
    assert code == 0 and report["sources"] == {"handbook": "updated"}
    assert [hit.path for hit in Engine(repo.root).search("quokka_scheduler").hits] == ["handbook:guides/new.md"]


def test_a_fetch_that_fails_is_a_line_of_the_report_and_the_rest_is_still_done(repo, tmp_path, capsys):
    sources_file = tmp_path / "gone.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(sources_file)], check=True)
    docs_seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(docs_seed)], check=True)
    (docs_seed / "a.md").write_text("# A\n\nmarmot_ledger\n")
    for args in (["add", "-A"], ["-c", "user.email=t@example.com", "-c", "user.name=T", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "a"],
                 ["push", "-q", str(sources_file), "HEAD:refs/heads/main"]):
        subprocess.run(["git", "-C", str(docs_seed), *args], check=True)
    sources.add(repo.root, sources_file.as_uri(), name="notes")
    subprocess.run(["rm", "-rf", str(sources_file)], check=True)   # the origin is gone: the network is away, as far as Aleph can tell
    repo.write("src/billing/tax.py", "def vat_rate(country):\n    return 0.2\n")
    repo.commit("a merged pull request")
    code, report = _report(repo, capsys)
    assert code == 0 and report["indexed"] >= 1 and report["pending"] == 0 and report["failed"] == []     # not a failure of the command
    assert "notes" in report["sources"] and report["sources"]["notes"] != "updated"
    assert [hit.path for hit in Engine(repo.root).search("marmot_ledger").hits] == ["notes:a.md"]


def test_the_graph_s_facts_are_read_where_they_were_in_use_and_not_begun_where_they_were_not(repo, capsys):
    from aleph_zero import callgraph
    from aleph_zero.indexer import Index
    from aleph_zero.store import index_path, open_db
    from aleph_zero.view import build_view

    def graph_rows():
        conn = open_db(index_path(repo.root))
        try:
            return conn.execute("SELECT count(*) FROM graph_facts").fetchall()[0][0]
        finally:
            conn.close()

    code, report = _report(repo, capsys)
    assert graph_rows() == 0 and report["graph"]["read"] == 0 and "never asked" in report["graph"]["why"]
    assert cli.main(["--cwd", str(repo.root), "callers", "refresh_token"]) == 0        # someone asks the graph once
    before = graph_rows()
    assert before > 0
    repo.write("src/billing/tax.py", "def vat_rate(country):\n    return refresh_token(country)\n")
    repo.commit("a merged pull request")
    code, report = _report(repo, capsys)
    assert code == 0 and report["graph"]["read"] == 1 and graph_rows() == before + 1
    index = Index(open_db(index_path(repo.root)))
    index.sync(build_view(repo.root))
    assert callgraph.pending(index) == []
    index.conn.close()


def test_only_one_update_at_a_time_and_the_second_says_so_without_an_error(repo, capsys):
    with update.only_one(gitio.common_dir(repo.root)) as mine:
        assert mine is True
        code, report = _report(repo, capsys)
        assert code == 0 and report == {"busy": "another `aleph update` is running for this repository"}
    assert "busy" not in _report(repo, capsys)[1]


def test_quiet_says_nothing_when_all_went_well(repo, capsys):
    assert _update(repo, "--quiet") == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_the_hook_lines_are_printed_and_nothing_is_installed(repo, capsys):
    import sys
    from pathlib import Path

    assert _update(repo, "--print-hook") == 0
    out = capsys.readouterr().out
    engine = Path(sys.executable).with_name("aleph")
    assert f"{engine} update --quiet" in out and "--minutes" in out and "post-merge" in out and "post-checkout" in out
    assert '[ "$3" = "1" ]' in out and "&" in out                  # a change of branch only, and detached: git returns at once
    hooks = Path(gitio.git(repo.root, "rev-parse", "--path-format=absolute", "--git-path", "hooks").decode().strip())
    assert not (hooks / "post-merge").exists() and not (hooks / "post-checkout").exists()
    assert Engine(repo.root).status()["pending"] > 0               # and nothing was updated: it only prints


def test_the_setup_hint_is_never_said_by_this_command(repo, capsys, monkeypatch):
    monkeypatch.setattr(firstrun, "hint_wanted", lambda: True)
    assert _update(repo) == 0
    assert "aleph setup" not in capsys.readouterr().err and "update" not in cli.HINTED


def test_a_directory_that_is_not_a_repository_is_indexed_and_the_rest_is_said_not_to_apply(tmp_path, capsys):
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "todo.py").write_text("def later():\n    return 1\n")
    assert cli.main(["--cwd", str(plain), "update", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["indexed"] == 1 and report["sources"] == {} and report["failed"] == []


def test_git_s_own_variables_from_a_hook_do_not_turn_aleph_to_another_repository(repo, make_repo, tmp_path, monkeypatch):
    other = make_repo(tmp_path / "other")
    other.write("x.py", "def only_in_other():\n    return 1\n")
    other.commit("other")
    monkeypatch.setenv("GIT_DIR", str(other.root / ".git"))        # as git sets it for the hooks of `other`
    monkeypatch.setenv("GIT_WORK_TREE", str(other.root))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other.root / ".git" / "index"))
    assert gitio.repo_info(repo.root).root == repo.root.resolve()
    hits = Engine(repo.root).search("render_invoice").hits
    assert hits and hits[0].path == "src/billing/invoice.py" and Engine(repo.root).search("only_in_other").hits == []
