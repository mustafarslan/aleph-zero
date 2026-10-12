import json
import subprocess
import time

import pytest

from aleph_zero import cli, sources
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


def test_a_path_source_follows_that_clones_working_tree(repo, docs_repo):
    sources.add(repo.root, str(docs_repo.root), name="docs")
    engine = Engine(repo.root)
    engine.search("anything")
    docs_repo.write("guides/auth.md", "# Authentication\n\nNow handled by the numbat_gateway.\n")  # not committed
    assert paths(engine.search("numbat_gateway")) == ["docs:guides/auth.md"]
    assert paths(engine.search("wombat_rotator")) == []


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
