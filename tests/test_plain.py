"""A directory that is not a git repository: searched as it is on disk, and refused where walking it would be a mistake."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from aleph_zero import cli, doctor, gitio, plain
from aleph_zero.engine import Engine
from aleph_zero.view import build_view


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A small project with no `.git`, and a home directory that is not the real one."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    root = tmp_path / "work" / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "tokens.py").write_text("def refresh_token(store):\n    return store.rotate()\n")
    (root / "src" / "invoice.ts").write_text("export function renderInvoice(order) { return order.total; }\n")
    (root / "notes.md").write_text("# Notes\n\nTokens are rotated on refresh.\n")
    return root


def under(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def test_a_directory_without_git_is_searched_as_it_is_on_disk_and_nothing_is_written_into_it(project):
    before = under(project)
    engine = Engine(project)
    assert [hit.path for hit in engine.search("refresh token").hits][0] == "src/tokens.py"
    assert [hit.path for hit in engine.symbol("renderInvoice").symbols] == ["src/invoice.ts"]
    (project / "src" / "tokens.py").write_text("def refresh_token(store):\n    return store.rotate_quokka()\n")
    (project / "src" / "fresh.py").write_text("def brand_new_wombat():\n    return 1\n")
    (project / "notes.md").unlink()
    assert [hit.path for hit in engine.search("rotate_quokka").hits] == ["src/tokens.py"]           # an edit
    assert [hit.path for hit in engine.search("brand_new_wombat").hits] == ["src/fresh.py"]         # a new file
    assert "notes.md" not in [hit.path for hit in engine.search("rotated on refresh").hits]         # a deleted one
    assert under(project) == (before | {"src/fresh.py"}) - {"notes.md"}                              # only what this test wrote
    kept = Path(os.environ["XDG_CACHE_HOME"]) / "aleph-zero"
    assert list(kept.rglob("index.db")) and not list(project.rglob("index.db"))


def test_what_a_directory_without_git_leaves_out(project):
    (project / ".env").write_text("SECRET=1\n")
    (project / ".config" / "x.py").parent.mkdir()
    (project / ".config" / "x.py").write_text("def hidden(): ...\n")
    for generated in ("node_modules/pkg/index.js", "__pycache__/x.py", "venv/lib/site.py", "target/debug/out.rs"):
        (project / generated).parent.mkdir(parents=True)
        (project / generated).write_text("x = 1\n")
    (project / "target" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n")
    (project / "build").mkdir()
    (project / "build" / "made.py").write_text("def made(): ...\n")                 # may be source: not left out by its name
    (project / "linked.py").symlink_to(project / "src" / "tokens.py")
    (project / "linked").symlink_to(project / "src", target_is_directory=True)
    inner = project / "vendor" / "lib"
    inner.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(inner)], check=True)
    (inner / "lib.py").write_text("def theirs(): ...\n")
    view = build_view(project)
    assert sorted(view.files) == ["build/made.py", "notes.md", "src/invoice.ts", "src/tokens.py"]
    assert view.plain and view.inner_repositories == 1
    status = Engine(project).status()
    assert status["repository"] is False and status["inner_repositories"] == 1


def test_where_walking_would_be_a_mistake_nothing_is_read(project, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "stray.py").write_text("x = 1\n")
    for refused in (Path("/"), Path("/usr"), home, tmp_path):                       # the root, a directory in it, home, what holds home
        assert plain.refusal(refused.resolve()), refused
    assert plain.refusal(project.resolve()) is None
    monkeypatch.setattr(os, "scandir", lambda *_: (_ for _ in ()).throw(AssertionError("walked")))
    for refused in (home, tmp_path):
        with pytest.raises(gitio.NotARepository, match="home directory"):
            build_view(refused)


def test_a_directory_too_large_is_refused_whole_and_the_walk_stops_at_the_limit(project, monkeypatch, capsys):
    for number in range(6):
        (project / f"extra{number}.py").write_text(f"x = {number}\n")
    monkeypatch.setattr(plain, "FILE_LIMIT", 5)
    with pytest.raises(gitio.NotARepository, match="more than 5 files"):
        build_view(project)
    assert cli.main(["--cwd", str(project), "search", "refresh"]) == 2 and "git init" in capsys.readouterr().err
    assert not list((Path(os.environ["XDG_CACHE_HOME"])).rglob("index.db"))       # no part of it was indexed
    monkeypatch.setattr(plain, "FILE_LIMIT", 20_000)
    monkeypatch.setattr(plain, "ENTRY_LIMIT", 4)
    looked = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda path: looked.append(path) or real(path))
    with pytest.raises(gitio.NotARepository, match="more than 4 entries"):
        build_view(project)
    assert len(looked) == 1                                                        # it did not read on to say so


def test_a_folder_of_repositories_is_not_a_project(project, tmp_path):
    folder = tmp_path / "work" / "many"
    for name in ("one", "two"):
        (folder / name).mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(folder / name)], check=True)
        (folder / name / "a.py").write_text("def a(): ...\n")
    with pytest.raises(gitio.NotARepository, match="2 git repositories"):
        build_view(folder)
    (folder / "README.md").write_text("# Many\n")
    view = build_view(folder)
    assert sorted(view.files) == ["README.md"] and view.inner_repositories == 2
    assert build_view(folder / "one").plain is False                               # a repository is a repository


def test_only_git_saying_there_is_no_repository_opens_this_path(project, tmp_path, monkeypatch):
    bare = tmp_path / "work" / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    broken = tmp_path / "work" / "broken"
    (broken / ".git").mkdir(parents=True)                                           # a `.git` that git cannot read
    (broken / "a.py").write_text("x = 1\n")
    walking, real = {"allowed": False}, plain.read

    def read(directory):
        assert walking["allowed"], f"walked {directory} though git did not say it is in no repository"
        return real(directory)

    monkeypatch.setattr(plain, "read", read)
    for directory in (bare, bare / "objects"):
        with pytest.raises(gitio.GitError):
            build_view(directory)
    walking["allowed"] = True
    assert build_view(broken).plain                                                 # git says: not a repository. So it is a directory
    walking["allowed"] = False
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))                           # no git at all: an error, as before
    with pytest.raises((gitio.GitError, OSError)):
        build_view(project)


def test_a_file_is_hashed_again_only_when_it_may_have_changed(project, monkeypatch):
    build_view(project)
    hashed = []
    real = gitio.hash_blob
    monkeypatch.setattr(gitio, "hash_blob", lambda data, fmt="sha1": hashed.append(len(data)) or real(data, fmt))
    plain.forget()                                                                  # as a new process: only the file beside the index remembers
    first = build_view(project)
    assert hashed == []                                                             # sizes and times as remembered: nothing is read
    target = project / "src" / "tokens.py"
    was = target.stat()
    target.write_text("def refresh_token(store):\n    return store.rotatQ()\n")     # the same size ...
    os.utime(target, ns=(was.st_atime_ns, was.st_mtime_ns))                         # ... and the same modification time
    assert target.stat().st_size == was.st_size and target.stat().st_mtime_ns == was.st_mtime_ns
    second = build_view(project)
    assert len(hashed) == 1 and second.files["src/tokens.py"] != first.files["src/tokens.py"]
    engine = Engine(project)
    assert "rotatQ" in engine.render(engine.search("rotatQ"), detail="full")                        # the new bytes are what is searched


def test_a_file_that_goes_away_between_the_walk_and_the_read_is_not_an_error(project):
    view = build_view(project)
    (project / "notes.md").unlink()
    (project / "src" / "invoice.ts").unlink()
    (project / "src" / "invoice.ts").mkdir()
    with gitio.Readers() as readers:
        assert view.read("notes.md", readers) is None and view.read("src/invoice.ts", readers) is None
    assert [hit.path for hit in Engine(project).search("refresh token").hits] == ["src/tokens.py"]


def test_every_command_works_or_says_why_not(project, capsys):
    run = lambda *arguments: (cli.main(["--cwd", str(project), *arguments]), capsys.readouterr())   # noqa: E731
    for arguments in (("search", "refresh token"), ("symbol", "refresh_token"), ("outline", "src/tokens.py"), ("callees", "refresh_token"),
                      ("facts",), ("decisions", "tokens"), ("index",), ("status",), ("gc",)):
        code, said = run(*arguments)
        assert code == 0, (arguments, said.err)
    code, said = run("status")
    assert json.loads(said.out)["repository"] is False and "hidden" in json.loads(said.out)["left_out"]
    rows = {row.name: row for row in doctor.check(project)}
    assert "not a git repository" in rows["repository"].detail and "hidden" in rows["repository"].detail
    assert rows["index"].level != "FAIL"
    for arguments in (("scip", "run", "--lang", "python"), ("source", "add", "https://example.invalid/x.git")):
        code, said = run(*arguments)
        assert code != 0 and "repository" in said.err, (arguments, said.err)


def test_the_server_answers_in_a_directory_without_git(project):
    from test_server import Client

    client = Client(project, env={"HOME": os.environ["HOME"]})
    try:
        client.start()
        assert "src/tokens.py" in client.text("search", query="refresh token")
        assert json.loads(client.text("status", format="json"))["repository"] is False
    finally:
        client.close()
