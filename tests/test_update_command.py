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
    assert report["sources"]["notes"].startswith("failed") and "fetched" in report["sources"]["notes"]      # and how old what is searched is
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
    with update.only_one(gitio.common_dir(repo.root), repo.root) as mine:
        assert mine is True
        code, report = _report(repo, capsys)
        assert code == 0 and report["busy"] == "another `aleph update` is running for this worktree" and report["indexed"] == 0
    assert _report(repo, capsys)[1]["busy"] is None


def test_quiet_says_nothing_when_all_went_well(repo, capsys):
    assert _update(repo, "--quiet") == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_the_hook_lines_are_printed_and_nothing_is_installed(repo, capsys):
    import sys
    from pathlib import Path

    assert _update(repo, "--print-hook") == 0
    out = capsys.readouterr().out
    engine = Path(sys.executable).with_name("aleph-zero")
    assert f"{engine} update --quiet" in out and "--minutes" in out and "post-merge" in out and "post-checkout" in out
    assert '[ "$3" = "1" ] || exit 0' in out and "& )" in out and "--no-fetch" in out   # a change of branch only, detached, and no network from a hook
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


# ---- from the review of the branch (2026-10-13) ---------------------------------------------------


def _hook(tmp_path, name):
    """The lines printed for one hook, as a file sh can run, with the engine replaced by a command that leaves a mark."""
    import re

    mark = tmp_path / f"{name}.ran"
    printed = update.hook_lines()
    body = printed.split(f"# .git/hooks/{name}\n", 1)[1].split("\n\n", 1)[0]
    body = re.sub(r"\( \S+ update [^>]*", f"( touch {mark} ", body)       # what would run, without running an update
    script = tmp_path / name
    script.write_text(body + "\n")
    script.chmod(0o755)
    return script, mark


def test_the_printed_hooks_run_under_sh_and_never_fail_git(tmp_path):
    import time

    merge, merged = _hook(tmp_path, "post-merge")
    assert subprocess.run(["sh", str(merge), "0"]).returncode == 0
    checkout, switched = _hook(tmp_path, "post-checkout")
    assert subprocess.run(["sh", str(checkout), "old", "new", "0"]).returncode == 0     # one restored file: nothing to do, and no failure
    time.sleep(0.5)
    assert merged.exists() and not switched.exists()
    assert subprocess.run(["sh", str(checkout), "old", "new", "1"]).returncode == 0     # a change of branch
    time.sleep(0.5)
    assert switched.exists()


def test_the_engine_s_path_is_quoted_in_the_printed_lines(tmp_path, monkeypatch):
    import sys

    tools = tmp_path / "some one" / "venv" / "bin"
    tools.mkdir(parents=True)
    (tools / "aleph-zero").write_text("#!/bin/sh\n")
    monkeypatch.setattr(sys, "executable", str(tools / "python"))
    assert f"'{tools / 'aleph-zero'}' update" in update.hook_lines()


def test_a_fetch_nobody_watches_does_not_ask_and_does_not_carry_the_caller_s_options(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "http.extraHeader=Authorization: secret")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: secret")
    usual = gitio._env()
    assert "GIT_CONFIG_PARAMETERS" in usual and "GIT_TERMINAL_PROMPT" not in usual      # a person's own command is as they gave it
    with gitio.unattended():
        quiet = gitio._env()
    assert quiet["GIT_TERMINAL_PROMPT"] == "0" and quiet["GIT_ASKPASS"] == "true" and "BatchMode=yes" in quiet["GIT_SSH_COMMAND"]
    assert not any(name.startswith("GIT_CONFIG_") for name in quiet)                    # options given to `git pull` stay with that pull
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/somewhere/gitconfig")                     # the user's own configuration file is not such an option
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /my/key")
    with gitio.unattended():
        assert gitio._env()["GIT_CONFIG_GLOBAL"] == "/somewhere/gitconfig" and gitio._env()["GIT_SSH_COMMAND"] == "ssh -i /my/key"
    assert "GIT_CONFIG_PARAMETERS" in gitio._env()                                      # and only inside the block


def test_the_report_has_the_same_keys_whatever_happened(repo, capsys):
    keys = {"busy", "sources", "indexed", "skipped", "pending", "graph", "architecture", "compiler_indexes", "vectors", "rebuilt", "failed"}
    assert set(_report(repo, capsys)[1]) == keys
    with update.only_one(gitio.common_dir(repo.root), repo.root) as mine:
        assert mine is True
        code, report = _report(repo, capsys)
    assert code == 0 and set(report) == keys and report["busy"] == "another `aleph update` is running for this worktree"


def test_another_worktree_of_the_repository_is_not_held_up(repo, tmp_path, capsys):
    other = tmp_path / "other"
    repo.git("worktree", "add", "-q", "--detach", str(other))
    with update.only_one(gitio.common_dir(repo.root), repo.root):
        assert cli.main(["--cwd", str(other), "update", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["busy"] is None


def test_an_index_that_is_held_by_another_writer_is_a_line_and_no_traceback(repo, capsys, monkeypatch):
    import apsw

    from aleph_zero.indexer import Index

    def held(self, *args, **kwargs):
        raise apsw.BusyError("database is locked")

    monkeypatch.setattr(Index, "sync", held)
    assert _update(repo, "--quiet") == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "FAILED index" in captured.err and "locked" in captured.err and "Traceback" not in captured.err


def test_what_changed_while_an_update_ran_is_taken_up_by_the_same_update(repo, capsys, monkeypatch):
    from aleph_zero.indexer import Index

    real = Index.sync
    state = {"calls": 0}

    def sync(self, view, budget_seconds=30.0, **kwargs):
        state["calls"] += 1
        done = real(self, view, budget_seconds=budget_seconds, **kwargs)
        if state["calls"] == 1:                                    # a checkout lands while the first round is at work
            repo.write("src/billing/late.py", "def arrived_late():\n    return 1\n")
            repo.commit("landed meanwhile")
        return done

    monkeypatch.setattr(Index, "sync", sync)
    code, report = _report(repo, capsys)
    monkeypatch.setattr(Index, "sync", real)
    assert code == 0 and Engine(repo.root).status()["pending"] == 0
    assert [hit.path for hit in Engine(repo.root).search("arrived_late").hits] == ["src/billing/late.py"]


def test_where_the_verdict_is_for_vectors_they_are_made_and_a_no_kept_for_the_repository_is_obeyed(repo, with_worker, capsys, monkeypatch):
    monkeypatch.setattr(dense_policy, "VERDICTS", {"python": Verdict(True, "test")})
    code, report = _report(repo, capsys)
    assert report["vectors"]["made"] > 0 and report["vectors"]["left"] == 0
    settings.change(settings.repo_file(gitio.common_dir(repo.root)), dense=False)
    repo.write("src/billing/tax.py", "def vat_rate(country):\n    return 0.2\n")
    repo.commit("a merged pull request")

    def never(*args, **kwargs):
        raise AssertionError("the worker was asked although vectors are off here")

    monkeypatch.setattr("aleph_zero.embed.client.EmbedClient.embed", never)
    monkeypatch.setattr(worker, "ensure", never)
    monkeypatch.setattr(worker, "socket_path", never)
    code, report = _report(repo, capsys)
    assert code == 0 and report["vectors"]["made"] == 0 and "this repository" in report["vectors"]["why"]
    monkeypatch.setenv("ALEPH_DENSE", "0")                         # and for one process, as for search and serve
    settings.change(settings.repo_file(gitio.common_dir(repo.root)), dense=None)
    code, report = _report(repo, capsys)
    assert report["vectors"]["made"] == 0 and "by hand" in report["vectors"]["why"]


def test_minutes_bound_the_embedding_and_what_is_left_is_said(repo, with_worker, capsys, monkeypatch):
    settings.change(settings.repo_file(gitio.common_dir(repo.root)), dense=True)
    code, report = _report(repo, capsys, "--minutes", "0")
    assert code == 0 and report["vectors"]["made"] + report["vectors"]["left"] > 0     # at least a batch, and the rest is named
    assert "vector" in "\n".join(update.text(report))


# ---- from the second review (agy with boost, 2026-10-13): each was checked before it was believed ----


def _run_hook(tmp_path, repo, name, *args):
    script, mark = _hook(tmp_path, name)
    done = subprocess.run(["sh", str(script), *args], cwd=repo.root)
    import time

    time.sleep(0.4)
    ran = mark.exists()
    mark.unlink(missing_ok=True)
    return done.returncode, ran


def test_the_printed_hook_stays_out_of_a_rebase_and_a_bisect(tmp_path, repo):
    assert _run_hook(tmp_path, repo, "post-checkout", "a", "b", "1") == (0, True)
    (repo.root / ".git" / "rebase-merge").mkdir()                  # git is in the middle of a rebase
    assert _run_hook(tmp_path, repo, "post-checkout", "a", "b", "1") == (0, False)
    (repo.root / ".git" / "rebase-merge").rmdir()
    (repo.root / ".git" / "BISECT_LOG").write_text("git bisect start\n")       # a bisect checks out a commit a step
    assert _run_hook(tmp_path, repo, "post-checkout", "a", "b", "1") == (0, False)
    assert _run_hook(tmp_path, repo, "post-merge", "0") == (0, False)
    (repo.root / ".git" / "BISECT_LOG").unlink()
    assert _run_hook(tmp_path, repo, "post-merge", "0") == (0, True)
    printed = update.hook_lines()
    assert "</dev/null" in printed and "git rev-parse --git-path hooks" in printed      # nothing read from the terminal; where hooks live in a linked worktree


def test_the_engine_is_named_by_where_it_is_or_by_where_the_shell_finds_it(tmp_path, monkeypatch):
    import sys

    assert firstrun.engine_path().endswith("/aleph-zero")          # here: beside the Python this runs under; never `aleph`, TeX's
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Python.app" / "Contents" / "MacOS" / "Python"))      # a Python that is an application
    monkeypatch.setattr("shutil.which", lambda name: f"/opt/tools/bin/{name}")
    assert firstrun.engine_path() == "/opt/tools/bin/aleph-zero"   # nothing beside that Python: what the shell would run
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert firstrun.engine_path() == "aleph-zero"                  # and failing that, its name, which at least says what is missing


def test_an_update_also_does_the_daily_collection_a_query_would_have_done(repo, capsys):
    import time

    from aleph_zero.store import index_path, open_db

    assert _update(repo) == 0
    (repo.root / "src" / "billing" / "invoice.py").unlink()
    repo.commit("the invoice is gone")
    conn = open_db(index_path(repo.root))
    long_ago = int(time.time()) - 40 * 86400
    conn.execute("UPDATE blobs SET last_seen_at = ?", (long_ago,))
    conn.execute("INSERT INTO meta(key, value) VALUES ('collected_at', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (long_ago,))
    before = conn.execute("SELECT count(*) FROM blobs").fetchall()[0][0]
    conn.close()
    repo.write("src/billing/tax.py", "def vat_rate(country):\n    return 0.2\n")
    repo.commit("a merged pull request")
    assert _update(repo) == 0                                      # with hooks, this is the only thing that ever indexes
    conn = open_db(index_path(repo.root))
    after = conn.execute("SELECT count(*) FROM blobs").fetchall()[0][0]
    conn.close()
    assert after == before                                         # one file came, and the one gone forty days ago was collected


def test_nothing_to_embed_means_the_worker_is_not_started(repo, with_worker, capsys, monkeypatch):
    settings.change(settings.repo_file(gitio.common_dir(repo.root)), dense=True)
    assert _report(repo, capsys)[1]["vectors"]["left"] == 0

    def never(*args, **kwargs):
        raise AssertionError("the worker was started although every chunk has its vector")

    monkeypatch.setattr(worker, "ensure", never)                   # a hook after every pull must not load a model for nothing
    code, report = _report(repo, capsys)
    assert code == 0 and report["vectors"] == {"made": 0, "left": 0, "why": ""} and report["failed"] == []


def test_the_bound_on_minutes_really_bounds(repo, with_worker, capsys):
    repo.write("src/many.py", "".join(f"def handler_{n}(request):\n    return request.value + {n}\n\n\n" for n in range(400)))
    repo.commit("four hundred functions")
    settings.change(settings.repo_file(gitio.common_dir(repo.root)), dense=True)
    code, report = _report(repo, capsys, "--minutes", "0")
    assert code == 0 and report["vectors"]["made"] > 0 and report["vectors"]["left"] > 0       # one batch, and the rest is named
    assert "still without one" in "\n".join(update.text(report))
    code, report = _report(repo, capsys)
    assert report["vectors"]["left"] == 0 and report["vectors"]["made"] > 0                    # and the next run makes the rest


def test_the_architecture_s_facts_are_read_where_they_were_in_use(repo, capsys):
    from aleph_zero.store import index_path, open_db

    def rows():
        conn = open_db(index_path(repo.root))
        try:
            return conn.execute("SELECT count(*) FROM arch_facts").fetchall()[0][0]
        finally:
            conn.close()

    code, report = _report(repo, capsys)
    assert rows() == 0 and "never asked" in report["architecture"]["why"]
    assert cli.main(["--cwd", str(repo.root), "facts"]) == 0       # someone asks for the facts once
    before = rows()
    assert before > 0
    repo.write("src/api.py", "import os\n\nDATABASE_URL = os.environ['DATABASE_URL']\n")
    repo.commit("a merged pull request")
    code, report = _report(repo, capsys)
    assert code == 0 and report["architecture"] == {"read": 1, "left": 0, "why": ""} and rows() == before + 1


def test_compiler_indexes_that_are_out_of_date_are_counted_and_never_run(repo, capsys, monkeypatch):
    from aleph_zero import scipstore

    class Stale:
        any = True

        def __init__(self, conn, files):
            self.files = files

        def status(self, path):
            return "the file changed since the index was made" if path.endswith("invoice.py") else ("" if path.endswith(".py") else None)

    monkeypatch.setattr(scipstore, "Compiled", Stale)
    code, report = _report(repo, capsys)
    assert code == 0 and report["compiler_indexes"]["out_of_date"] == 1 and "aleph scip run" in report["compiler_indexes"]["how"]
    assert any("aleph scip run" in line for line in update.text(report))


def test_an_update_builds_again_what_a_newer_aleph_pins_and_a_hook_s_update_does_not(repo, monkeypatch):
    from aleph_zero import aeon_setup, update
    from aleph_zero.embed import setup as embed_setup

    built = []
    monkeypatch.setattr(aeon_setup, "installed", lambda: True)
    monkeypatch.setattr(aeon_setup, "current", lambda: False)
    monkeypatch.setattr(aeon_setup, "install", lambda source=None, report=None: built.append("aeon"))
    monkeypatch.setattr(embed_setup, "installed", lambda: True)
    monkeypatch.setattr(embed_setup, "current", lambda: True)
    monkeypatch.setattr(embed_setup, "install_binary", lambda source=None, report=None: built.append("worker"))
    hook = update.run(repo.root, fetch=False)                          # a git hook's update: no network, no build
    assert built == [] and hook["rebuilt"] == []
    done = update.run(repo.root, fetch=True)
    assert built == ["aeon"] and done["rebuilt"] == ["Aeon's core"]
    assert any("Aeon's core" in line and "pinned" in line for line in update.text(done))
