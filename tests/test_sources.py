import json
import subprocess
import time

import pytest

from aleph_zero import cli, gitio, sources
from aleph_zero import view as views
from aleph_zero.engine import Engine


def paths(answer):
    return [hit.path for hit in answer.hits]


@pytest.fixture
def docs_repo(tmp_path, make_repo):
    docs = make_repo(tmp_path / "team-docs")
    docs.write("guides/auth.md", "# Authentication\n\nTokens are rotated by the wombat_rotator service.\n")
    docs.write("guides/billing.md", "# Billing\n\nInvoices are rendered nightly.\n")
    docs.commit("docs")
    return docs


def test_a_source_added_by_path_is_searchable_under_its_name(repo, docs_repo):
    sources.add(repo.root, str(docs_repo.root), name="docs")
    engine = Engine(repo.root)
    assert paths(engine.search("wombat_rotator")) == ["docs:guides/auth.md"]
    assert paths(engine.search("wombat_rotator", kind="docs", path_glob="docs:*")) == ["docs:guides/auth.md"]
    assert paths(engine.search("wombat_rotator", path_glob="src/*")) == []
    assert engine.outline("docs:guides/auth.md").items[0].name == "Authentication"
    status = engine.status()
    assert status["files_in_view"] == 5
    assert [(s["name"], s["kind"], s["files"]) for s in status["sources"]] == [("docs", "path", 2)]


def test_a_path_source_follows_that_clones_working_tree_within_a_few_seconds(repo, docs_repo, monkeypatch):
    # The owner's yes (2026-10-14): a linked checkout may be answered from a view up to PATH_SOURCE_SECONDS old. Its own
    # `git status` ran at every question: 70 ms for a source of 6,781 files. A stage, a commit or a checkout there is
    # seen at once; an edit not yet staged within the few seconds. The worktree itself is never answered from the past.
    clock = [1000.0]
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock[0])
    sources.forget_mounts()
    sources.add(repo.root, str(docs_repo.root), name="docs")
    engine = Engine(repo.root)
    engine.search("anything")
    docs_repo.write("guides/auth.md", "# Authentication\n\nNow handled by the numbat_gateway.\n")  # not committed
    assert paths(engine.search("numbat_gateway")) == []                                       # within the few seconds
    clock[0] += sources.PATH_SOURCE_SECONDS + 0.1
    assert paths(engine.search("numbat_gateway")) == ["docs:guides/auth.md"]
    assert paths(engine.search("wombat_rotator")) == []
    docs_repo.write("guides/billing.md", "# Billing\n\nInvoices go through the quokka_ledger.\n")
    docs_repo.git("add", "guides/billing.md")                                               # staged: seen at once
    assert paths(engine.search("quokka_ledger")) == ["docs:guides/billing.md"]
    repo.write("src/billing/invoice.py", "def render_invoice(order):\n    return 'the platypus_total'\n")
    assert "src/billing/invoice.py" in paths(engine.search("platypus_total"))                # the worktree's own edit, at once


def test_a_source_added_by_url_is_cloned_and_follows_refresh(repo, docs_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(docs_repo.root), str(remote)], check=True)
    url = remote.as_uri()
    added = sources.add(repo.root, url, name="handbook")
    assert added.kind == "url" and (tmp_path / "cache" / "aleph-zero" / "sources").is_dir()
    engine = Engine(repo.root)
    assert paths(engine.search("wombat_rotator")) == ["handbook:guides/auth.md"]

    docs_repo.write("guides/new.md", "# New\n\nThe quokka_scheduler is new.\n")
    docs_repo.commit("more docs")
    docs_repo.git("push", "-q", str(remote), "HEAD:refs/heads/main")
    assert paths(engine.search("quokka_scheduler")) == []          # as fresh as the last fetch
    report = sources.refresh(repo.root)
    assert report == {"handbook": "updated"}
    assert paths(engine.search("quokka_scheduler")) == ["handbook:guides/new.md"]


def test_an_unreachable_source_serves_its_last_good_state_and_says_how_old_it_is(repo, docs_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(docs_repo.root), str(remote)], check=True)
    sources.add(repo.root, remote.as_uri(), name="handbook")
    remote.rename(tmp_path / "gone.git")
    time.sleep(1.1)
    report = sources.refresh(repo.root)
    assert report["handbook"].startswith("failed")
    engine = Engine(repo.root)
    assert paths(engine.search("wombat_rotator")) == ["handbook:guides/auth.md"]
    listed, = engine.status()["sources"]
    assert listed["age_seconds"] >= 1 and "failed" in listed["last_error"]


def test_removing_a_source_removes_its_hits_and_a_missing_path_source_is_reported(repo, docs_repo, tmp_path):
    sources.add(repo.root, str(docs_repo.root), name="docs")
    engine = Engine(repo.root)
    assert paths(engine.search("wombat_rotator")) == ["docs:guides/auth.md"]
    sources.remove(repo.root, "docs")
    assert paths(engine.search("wombat_rotator")) == []
    sources.add(repo.root, str(docs_repo.root), name="docs")
    docs_repo.root.rename(tmp_path / "moved-away")
    assert paths(engine.search("wombat_rotator")) == []   # search keeps working without it
    assert "does not exist" in engine.status()["sources"][0]["last_error"]


def test_identical_content_in_the_repository_and_a_source_is_stored_once(repo, docs_repo):
    docs_repo.write("guide.md", (repo.root / "docs/guide.md").read_text())
    docs_repo.commit("same file")
    sources.add(repo.root, str(docs_repo.root), name="docs")
    engine = Engine(repo.root)
    hits = paths(engine.search("tokens expire", kind="docs"))
    assert "docs/guide.md" in hits and "docs:guide.md" in hits
    index = engine._indexes[repo.root.resolve()]
    same = index.conn.execute("SELECT count(*) FROM blobs WHERE family = 'markdown'").fetchall()[0][0]
    assert same == 3  # guide.md once, plus the two files only the source has


def test_source_names_and_duplicates_are_checked(repo, docs_repo):
    sources.add(repo.root, str(docs_repo.root))                    # the name defaults to the directory's
    assert [s.name for s in sources.load(repo.root)] == ["team-docs"]
    with pytest.raises(sources.SourceError, match="already"):
        sources.add(repo.root, str(docs_repo.root), name="team-docs")
    with pytest.raises(sources.SourceError, match="name"):
        sources.add(repo.root, str(docs_repo.root), name="has spaces")
    with pytest.raises(sources.SourceError, match="no source named"):
        sources.remove(repo.root, "nope")


def test_source_commands(repo, docs_repo, capsys):
    assert cli.main(["--cwd", str(repo.root), "source", "add", str(docs_repo.root), "--name", "docs"]) == 0
    assert "added docs (path)" in capsys.readouterr().out
    assert cli.main(["--cwd", str(repo.root), "source", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["name"] == "docs" and listed[0]["kind"] == "path"
    assert cli.main(["--cwd", str(repo.root), "search", "wombat_rotator", "--detail", "locations"]) == 0
    assert "docs:guides/auth.md" in capsys.readouterr().out
    assert cli.main(["--cwd", str(repo.root), "source", "remove", "docs"]) == 0
    assert cli.main(["--cwd", str(repo.root), "source", "remove", "docs"]) == 2


def test_two_repositories_linking_the_same_url_do_not_share_a_clone(repo, docs_repo, tmp_path, monkeypatch, make_repo):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(docs_repo.root), str(remote)], check=True)
    second = make_repo(tmp_path / "second-repo")
    second.write("a.py", "x = 1\n")
    second.commit("init")
    sources.add(repo.root, remote.as_uri(), name="hb")
    sources.add(second.root, remote.as_uri(), name="hb")
    sources.remove(repo.root, "hb")
    assert paths(Engine(second.root).search("wombat_rotator")) == ["hb:guides/auth.md"]


def test_a_real_file_spelled_like_a_source_path_is_indexed_and_wins(repo, docs_repo):
    docs_repo.write("guide.md", "# Linked\n\nThe linked one mentions linkedonlyword.\n")
    docs_repo.commit("guide")
    repo.write("docs:guide.md", "# Real\n\nThe real one mentions realonlyword.\n")
    sources.add(repo.root, str(docs_repo.root), name="docs")
    engine = Engine(repo.root)
    assert paths(engine.search("realonlyword")) == ["docs:guide.md"]
    assert engine.status()["pending"] == 0
    assert paths(engine.search("linkedonlyword")) == []  # hidden by the real file of the same spelling


def test_odd_source_lists_and_self_links(repo, docs_repo):
    with pytest.raises(sources.SourceError, match="this repository"):
        sources.add(repo.root, str(repo.root), name="me")
    sources.add(repo.root, str(docs_repo.root), name="docs")
    listing = repo.root / ".git" / "aleph-zero" / "sources.json"
    entries = json.loads(listing.read_text())
    entries[0]["added_by_a_newer_version"] = True       # an unknown key is ignored, not fatal
    listing.write_text(json.dumps(entries))
    engine = Engine(repo.root)
    assert paths(engine.search("wombat_rotator")) == ["docs:guides/auth.md"]
    listing.write_text("{ not json")
    status = engine.status()                             # status still answers and says what is wrong
    assert status["sources"] == [] and "sources.json" in status["sources_error"]


def test_with_git_dir_in_the_environment_a_source_s_commands_do_not_act_on_this_repository(repo, docs_repo, tmp_path, monkeypatch):
    """A repository used through `git --git-dir=… --work-tree=…` has GIT_DIR and GIT_WORK_TREE in the environment of
    everything started from it. The fetch of a linked source carried them along and so ran in THIS repository:
    with `--prune` and `+refs/heads/*:refs/heads/*` it replaced `main` and deleted the branches the remote lacks."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(docs_repo.root), str(remote)], check=True)
    repo.git("branch", "precious")
    repo.git("checkout", "-q", "-b", "work")
    before = {name: repo.git("rev-parse", name).strip() for name in ("main", "precious", "work")}
    monkeypatch.setenv("GIT_DIR", str(repo.root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo.root))
    sources.add(repo.root, remote.as_uri(), name="handbook")                       # cloned somewhere else, with the variables set
    docs_repo.write("guides/new.md", "# New\n\nThe quokka_scheduler is new.\n")
    monkeypatch.delenv("GIT_DIR"), monkeypatch.delenv("GIT_WORK_TREE")
    docs_repo.commit("more docs")
    docs_repo.git("push", "-q", str(remote), "HEAD:refs/heads/main")
    monkeypatch.setenv("GIT_DIR", str(repo.root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo.root))
    assert sources.refresh(repo.root) == {"handbook": "updated"}
    engine = Engine(repo.root)
    assert paths(engine.search("quokka_scheduler")) == ["handbook:guides/new.md"]  # read from the source's clone, not from here
    assert paths(engine.search("render_invoice"))[0] == "src/billing/invoice.py"   # and this repository is still read as itself
    monkeypatch.delenv("GIT_DIR"), monkeypatch.delenv("GIT_WORK_TREE")
    after = {name: repo.git("rev-parse", name).strip() for name in ("main", "precious", "work")}
    assert after == before                                                         # no branch replaced, none deleted
    assert repo.git("branch", "--list").split() == ["main", "precious", "*", "work"]


def test_a_source_linked_by_path_is_read_as_itself_with_git_dir_in_the_environment(repo, docs_repo, monkeypatch):
    sources.add(repo.root, str(docs_repo.root), name="docs")
    monkeypatch.setenv("GIT_DIR", str(repo.root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo.root))
    engine = Engine(repo.root)
    assert paths(engine.search("wombat_rotator")) == ["docs:guides/auth.md"]
    assert [row["files"] for row in sources.describe(gitio_common(repo))] == [2]


def gitio_common(repo):
    from aleph_zero import gitio

    return gitio.common_dir(repo.root)


# ---- from the second review of `aleph update` (agy with boost, 2026-10-13), each checked first ----


def test_a_source_added_while_a_refresh_is_fetching_is_not_lost(repo, docs_repo, tmp_path, make_repo, monkeypatch):
    from aleph_zero import gitio

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(docs_repo.root), str(remote)], check=True)
    sources.add(repo.root, remote.as_uri(), name="handbook")
    other = make_repo(tmp_path / "other-docs")
    other.write("notes.md", "# Notes\n\nThe numbat_queue drains at night.\n")
    other.commit("notes")
    real = gitio.git

    def fetch_and_meanwhile(cwd, *args):
        if "fetch" in args:                                        # a fetch takes up to a minute: someone links a source meanwhile
            sources.add(repo.root, str(other.root), name="notes")
        return real(cwd, *args)

    monkeypatch.setattr(gitio, "git", fetch_and_meanwhile)
    assert sources.refresh(repo.root) == {"handbook": "unchanged"}
    monkeypatch.setattr(gitio, "git", real)
    assert sorted(source.name for source in sources.load(repo.root)) == ["handbook", "notes"]


def test_many_writers_of_the_list_of_sources_leave_a_list(repo, tmp_path, make_repo):
    import threading

    from aleph_zero import gitio

    made = []
    for n in range(8):
        other = make_repo(tmp_path / f"docs-{n}")
        other.write("a.md", f"# {n}\n")
        other.commit("a")
        made.append(other)
    failures = []

    def link(other, n):
        try:
            sources.add(repo.root, str(other.root), name=f"docs-{n}")
        except Exception as exc:  # noqa: BLE001 - the test is that none happens
            failures.append(repr(exc))

    writers = [threading.Thread(target=link, args=(other, n)) for n, other in enumerate(made)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert failures == []
    assert sorted(source.name for source in sources.load(repo.root)) == sorted(f"docs-{n}" for n in range(8))
    assert not list((gitio.common_dir(repo.root) / "aleph-zero").glob("sources.json*tmp*"))       # no scratch file left behind


def test_a_git_command_that_runs_out_of_time_leaves_no_helper_behind(tmp_path):
    import socket
    import threading
    import time

    from aleph_zero import gitio

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    held = []

    def accept_and_say_nothing():
        try:
            while True:
                held.append(server.accept())
        except OSError:
            pass                                                   # the test closed the socket

    threading.Thread(target=accept_and_say_nothing, daemon=True).start()
    subprocess.run(["git", "init", "-q", str(tmp_path / "r")], check=True)
    started = time.monotonic()
    with pytest.raises(gitio.GitTimeout):
        with gitio.until(time.monotonic() + 2):
            gitio.git(tmp_path / "r", "ls-remote", f"http://127.0.0.1:{port}/nothing.git")   # a server that accepts and says nothing
    assert time.monotonic() - started < 6
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", f"127.0.0.1:{port}"], capture_output=True, text=True).stdout.split()
    subprocess.run(["pkill", "-f", f"127.0.0.1:{port}"])
    server.close()
    assert left == []                                              # git's own helper went with it


def test_a_commit_in_a_path_source_that_is_a_linked_worktree_changes_its_stamp(docs_repo, tmp_path):
    # A linked worktree's branches live in the repository's common directory, not in its own git directory.
    side = tmp_path / "docs-side"
    docs_repo.git("worktree", "add", "-q", "-b", "side", str(side))
    before = sources._git_stamp(str(side))
    first = docs_repo.git("rev-parse", "HEAD").strip()
    subprocess.run(["git", "-C", str(side), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "nothing"], check=True)
    time.sleep(0.01)
    subprocess.run(["git", "-C", str(side), "update-ref", "refs/heads/side", first], check=True)   # moved back: index untouched
    assert before is not None and sources._git_stamp(str(side)) != before


def test_a_path_source_s_kept_view_is_dropped_when_a_file_of_it_can_no_longer_be_read_as_it_saw_it(repo, docs_repo, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock[0])
    sources.forget_mounts()
    docs_repo.write("guides/auth.md", "# Authentication\n\nFirst draft about the numbat_gateway.\n")    # not committed
    sources.add(repo.root, str(docs_repo.root), name="docs")
    view = views.build_view(repo.root)
    assert sources._MOUNTS
    docs_repo.write("guides/auth.md", "# Authentication\n\nSecond draft.\n")                      # within the few seconds
    with gitio.Readers() as readers:
        assert view.read("docs:guides/auth.md", readers) is None
    assert not sources._MOUNTS                             # the next question looks at the source again


def test_letting_go_of_memory_drops_the_kept_views_of_path_sources(repo, docs_repo):
    sources.forget_mounts()
    sources.add(repo.root, str(docs_repo.root), name="docs")
    views.build_view(repo.root)
    assert sources._MOUNTS
    views.forget_trees()
    assert not sources._MOUNTS


def test_a_url_that_asks_for_a_login_is_said_to_be_missing_or_private():
    # GitHub answers a repository that does not exist (or is private) by asking git for a login.
    from aleph_zero import sources
    said = sources.why_not_cloned("https://github.com/someone/nothing", "fatal: could not read Username for "
                                  "'https://github.com': terminal prompts disabled")
    assert "does not exist" in said and "private" in said and "Username" not in said
    assert "timed out" in sources.why_not_cloned("u", "git timed out")      # anything else is passed on


def test_the_status_says_how_far_vectors_are_made_for_the_code_the_docs_and_each_linked_source(repo, docs_repo):
    # For a person watching vectors being made (the plugin's status line, /aleph configure): one count is not enough
    # when documentation is linked.
    repo.write("README.md", "# Shop\n\nHow to run the shop.\n")
    repo.commit("readme")
    sources.add(repo.root, str(docs_repo.root), name="team")
    engine = Engine(repo.root)
    engine.search("wombat_rotator")                                      # indexed
    vectors = engine.status()["vectors"]
    parts = {part["name"]: part for part in vectors["parts"]}
    assert [part["kind"] for part in vectors["parts"]] == ["code", "docs", "source"]
    assert set(parts) == {"code", "docs", "team"}
    assert sum(part["chunks"] for part in vectors["parts"]) == vectors["chunks"]
    assert all(part["with_vector"] == 0 and part["chunks"] > 0 for part in vectors["parts"])


def test_linking_a_source_indexes_it_at_once_so_its_vectors_can_be_made_without_waiting_for_a_search(repo, docs_repo, capsys):
    assert cli.main(["--cwd", str(repo.root), "source", "add", str(docs_repo.root), "--name", "team"]) == 0
    assert "indexed" in capsys.readouterr().out
    assert Engine(repo.root).status()["pending"] == 0


def test_a_linked_source_in_another_language_does_not_change_the_repository_s_main_language(repo, tmp_path, make_repo):
    # The main language picks the vectors verdict: linking a large Go repository to a Python one must not make it Go.
    go = make_repo(tmp_path / "service")
    for n in range(6):
        go.write(f"pkg/s{n}.go", "package pkg\n\n" + "".join(f"func Handler{n}_{m}() int {{ return {m} }}\n" for m in range(40)))
    go.commit("go")
    sources.add(repo.root, str(go.root), name="service")
    engine = Engine(repo.root)
    engine.search("refresh token")
    assert engine.status()["main_language"] == "python"
