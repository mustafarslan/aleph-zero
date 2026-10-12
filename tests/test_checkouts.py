"""Repositories that are not a plain full clone: sparse, partial, shallow, with submodules;
and a directory that is no repository at all."""
import json
import subprocess
from pathlib import Path

import pytest

from aleph_zero import cli, gitio, sources
from aleph_zero import view as views
from aleph_zero.engine import Engine
from aleph_zero.view import build_view


@pytest.fixture
def origin(repo):
    """The usual fixture repository, able to serve filtered clones."""
    repo.git("config", "uploadpack.allowFilter", "true")
    repo.git("config", "uploadpack.allowAnySHA1InWant", "true")
    return repo


def clone(origin, target: Path, *options: str) -> Path:
    subprocess.run(["git", "clone", "-q", *options, f"file://{origin.root}", str(target)], check=True, capture_output=True)
    return target


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


def paths(engine: Engine, query: str) -> list[str]:
    answer = engine.search(query)
    assert answer.pending == 0 and not answer.timed_out
    return [hit.path for hit in answer.hits]


# ---- sparse checkout ---------------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["cone", "no-cone", "sparse-index"])
def test_files_outside_a_sparse_checkout_are_not_in_the_view(origin, tmp_path, mode):
    root = clone(origin, tmp_path / "sparse")
    if mode == "cone":
        git(root, "sparse-checkout", "set", "docs")
    elif mode == "no-cone":
        git(root, "sparse-checkout", "set", "--no-cone", "/docs/")
    else:
        git(root, "sparse-checkout", "set", "--sparse-index", "docs")
    assert not (root / "src").exists()
    view = build_view(root)
    assert sorted(view.files) == ["docs/guide.md"]
    assert view.outside_checkout == 2
    engine = Engine(root)
    assert "src/auth/token_store.py" not in paths(engine, "refresh token")
    assert paths(engine, "invoices are rendered nightly") == ["docs/guide.md"]
    assert engine.status()["outside_checkout"] == 2


def test_changing_the_sparse_pattern_changes_the_view_without_a_commit(origin, tmp_path):
    root = clone(origin, tmp_path / "sparse")
    git(root, "sparse-checkout", "set", "docs")
    engine = Engine(root)
    assert paths(engine, "render invoice") in ([], ["docs/guide.md"])
    git(root, "sparse-checkout", "set", "src/billing")
    assert paths(engine, "render invoice")[0] == "src/billing/invoice.py"
    assert "docs/guide.md" not in paths(engine, "invoices are rendered nightly")
    git(root, "sparse-checkout", "disable")
    assert build_view(root).outside_checkout == 0
    assert "docs/guide.md" in paths(engine, "invoices are rendered nightly")


def test_a_full_checkout_never_asks_git_for_skipped_files(repo, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("asked for skip-worktree entries in a repository without sparse checkout")

    monkeypatch.setattr(gitio, "skip_worktree", fail)
    assert build_view(repo.root).outside_checkout == 0


def test_a_blobless_sparse_clone_is_searched_without_fetching_anything(origin, tmp_path):
    root = clone(origin, tmp_path / "partial", "--filter=blob:none", "--sparse")
    git(root, "sparse-checkout", "set", "docs")
    before = git(root, "count-objects", "-v")
    engine = Engine(root)
    assert paths(engine, "invoices are rendered nightly") == ["docs/guide.md"]
    assert engine.status()["pending"] == 0
    assert git(root, "count-objects", "-v") == before   # nothing was fetched on the quiet


# ---- partial and shallow clones ---------------------------------------------------------------------------

def test_a_treeless_clone_and_a_shallow_clone_are_searched_like_any_other(origin, tmp_path):
    origin.write("src/later.py", "def added_later():\n    return 2\n")
    origin.commit("second")
    for name, options in (("treeless", ["--filter=tree:0"]), ("shallow", ["--depth", "1"])):
        engine = Engine(clone(origin, tmp_path / name, *options))
        assert paths(engine, "added_later")[0] == "src/later.py"
        assert paths(engine, "refresh token")[0] == "src/auth/token_store.py"


def test_a_blob_git_does_not_have_is_reported_and_does_not_hold_up_answers(repo):
    """An object missing from the store (a partial clone that lost its remote, a damaged
    repository). The file cannot be indexed; everything else is, and no answer waits for it."""
    oid = repo.git("rev-parse", "HEAD:src/billing/invoice.py").strip()
    (repo.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    engine = Engine(repo.root)
    answer = engine.search("refresh token", timeout_ms=5000)
    assert [hit.path for hit in answer.hits][0] == "src/auth/token_store.py"
    assert answer.pending == 0 and not answer.timed_out
    status = engine.status()
    assert status["pending"] == 0 and status["unreadable"] == 1
    assert "src/billing/invoice.py" not in paths(engine, "render invoice")
    # Once git has the object again (a fetch, a repair), the file is indexed.
    repo.git("hash-object", "-w", "src/billing/invoice.py")
    engine = Engine(repo.root)
    assert paths(engine, "render invoice")[0] == "src/billing/invoice.py"
    assert engine.status()["unreadable"] == 0


# ---- submodules -----------------------------------------------------------------------------------------------

@pytest.fixture
def with_submodule(repo, make_repo, tmp_path):
    library = make_repo(tmp_path / "library")
    library.write("lib/quokka.py", "def quokka_helper():\n    return 'from the library'\n")
    library.commit("library")
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", f"file://{library.root}", "vendor/lib")
    repo.commit("add the library")
    return repo


def test_submodules_are_not_searched_and_status_says_how_to_link_one(with_submodule):
    engine = Engine(with_submodule.root)
    assert all("quokka" not in path for path in paths(engine, "quokka_helper"))
    status = engine.status()
    assert status["submodules"] == ["vendor/lib"]
    assert "aleph source add vendor/lib" in status["submodules_note"]


def test_a_checked_out_submodule_can_be_linked_as_a_source(with_submodule):
    sources.add(with_submodule.root, str(with_submodule.root / "vendor/lib"))
    assert paths(Engine(with_submodule.root), "quokka_helper")[0] == "lib:lib/quokka.py"


def test_doctor_mentions_submodules_and_a_sparse_checkout(with_submodule, capsys):
    cli.main(["--cwd", str(with_submodule.root), "doctor"])
    out = capsys.readouterr().out
    assert "vendor/lib" in out and "aleph source add" in out


# ---- not a repository ----------------------------------------------------------------------------------------------

def test_a_directory_that_must_not_be_walked_gets_one_clear_sentence(tmp_path, capsys, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.py").write_text("def lonely():\n    return 1\n")
    assert [hit.path for hit in Engine(plain).search("lonely").hits] == ["notes.py"]     # a project without git is searched (M10)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: plain))                    # ... a home directory is not
    with pytest.raises(gitio.NotARepository) as caught:
        Engine(plain).search("lonely")
    assert "git init" in str(caught.value) and str(plain) in str(caught.value)
    assert cli.main(["--cwd", str(plain), "search", "lonely"]) == 2
    err = capsys.readouterr().err
    assert "git init" in err and "Traceback" not in err and "fatal:" not in err
    assert cli.main(["--cwd", str(plain), "status"]) == 2


def test_the_server_stays_up_in_a_directory_it_does_not_walk(tmp_path):
    from test_server import Client

    plain = tmp_path / "plain"
    plain.mkdir()
    # HOME in the environment, for once: the server is another process. It refuses before it parses anything,
    # so the grammar package never looks for its cache in this empty home (see conftest's check at the end of a run).
    client = Client(plain, env={"HOME": str(plain)})
    try:
        client.start()
        for tool, arguments in (("search", {"query": "anything"}), ("status", {}), ("symbol", {"name": "x"})):
            result = client.call(tool, **arguments)["result"]
            assert result["isError"] is True and "git init" in result["content"][0]["text"]
        assert client.request("tools/list")["result"]["tools"]
    finally:
        client.close()


def test_doctor_says_what_a_partial_clone_means_for_search(origin, tmp_path, capsys):
    root = clone(origin, tmp_path / "partial", "--filter=blob:none")
    cli.main(["--cwd", str(root), "doctor"])
    out = capsys.readouterr().out
    assert "partial clone" in out and "unreadable" in out
    cli.main(["--cwd", str(origin.root), "doctor"])
    assert "partial clone" not in capsys.readouterr().out
