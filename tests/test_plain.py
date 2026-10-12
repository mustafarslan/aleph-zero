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
    """A small project with no `.git`, and a home directory that is not the real one. The home is changed for
    this process's `Path.home()` only, not in the environment: the grammar package keeps its cache under HOME
    and would fetch every grammar again, over the network, into a home that has none."""
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
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
    def walked(root, *_):
        raise AssertionError(f"walked {root}, which is a repository or may be one")

    monkeypatch.setattr(plain, "_walk", walked)
    for directory in (bare, bare / "objects"):
        with pytest.raises(gitio.GitError):
            build_view(directory)
    with pytest.raises(gitio.GitError, match="cannot read the repository"):         # a `.git` is there: git's word is not enough
        build_view(broken)
    real_repo = tmp_path / "work" / "real"
    (real_repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(real_repo)], check=True)
    (real_repo / ".gitignore").write_text("ignored/\n")
    (real_repo / "src" / "a.py").write_text("def tracked(): ...\n")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(real_repo))                   # git no longer looks above `src`
    with pytest.raises(gitio.GitError, match="cannot read the repository"):
        build_view(real_repo / "src")
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere.git"))                    # left behind by a hook or a tool
    with pytest.raises(gitio.GitError, match="cannot read the repository"):
        build_view(real_repo)
    monkeypatch.delenv("GIT_DIR")
    (real_repo / ".git" / "HEAD").unlink()                                          # half deleted
    with pytest.raises(gitio.GitError, match="cannot read the repository"):
        build_view(real_repo)
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))                           # no git at all: one sentence, and no walk
    with pytest.raises(gitio.GitError, match="git is not installed"):
        build_view(project)
    assert cli.main(["--cwd", str(project), "search", "refresh"]) == 2


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
    for arguments in (("scip", "run", "--lang", "python"), ("source", "add", "https://example.invalid/x.git"), ("source", "list")):
        code, said = run(*arguments)
        assert code != 0 and "is not a git repository" in said.err and "rev-parse" not in said.err, (arguments, said.err)


def test_the_server_answers_in_a_directory_without_git(project):
    from test_server import Client

    client = Client(project)
    try:
        client.start()
        assert "src/tokens.py" in client.text("search", query="refresh token")
        assert json.loads(client.text("status", format="json"))["repository"] is False
    finally:
        client.close()


def test_the_home_directory_is_itself_however_its_path_is_spelled(project, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "stray.py").write_text("def stray(): ...\n")
    shouted = tmp_path / "HOME"
    if not shouted.exists():
        pytest.skip("this file system tells HOME from home")
    for spelled in (shouted, tmp_path / "Home" / ".." / "HOME", tmp_path / "WORK" / ".." / "home"):
        with pytest.raises(gitio.NotARepository, match="home directory"):
            build_view(spelled)
    assert plain.read(project)[1] == plain.read(tmp_path / "WORK" / "PROJECT")[1]      # one directory, one index


def test_without_a_home_directory_to_keep_out_of_nothing_is_walked(tmp_path, monkeypatch):
    directory = tmp_path / "work" / "somewhere"
    directory.mkdir(parents=True)
    (directory / "a.py").write_text("x = 1\n")
    monkeypatch.setenv("HOME", "")                      # refused before a single file is read (or a grammar wanted)
    assert str(Path.home()) == "/"
    with pytest.raises(gitio.NotARepository, match="home directory is not known"):
        build_view(directory)


def test_the_top_of_a_disk_and_the_temporary_directories_are_not_walked(project, monkeypatch):
    for refused in ("/private/tmp", "/tmp", "/var/tmp"):
        if Path(refused).exists():
            assert plain.refusal(Path(refused).resolve()), refused
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == project.resolve())
    with pytest.raises(gitio.NotARepository, match="top of a disk"):
        build_view(project)


def test_a_query_out_of_time_keeps_what_it_hashed(project, monkeypatch):
    for number in range(200):
        (project / f"m{number:03d}.py").write_text(f"x = {number}\n")
    hashed = []
    real = gitio.hash_blob
    monkeypatch.setattr(gitio, "hash_blob", lambda data, fmt="sha1": hashed.append(len(data)) or real(data, fmt))
    start = [0]

    def out_of_time_after_fifty_files():
        if len(hashed) - start[0] >= 50:
            raise gitio.GitTimeout("reading the worktree timed out before the time limit")

    monkeypatch.setattr(gitio, "check_deadline", out_of_time_after_fifty_files)
    reached = []
    for _ in range(2):                                  # each attempt runs out of time, and each gets further
        start[0] = len(hashed)
        with pytest.raises(gitio.GitTimeout):
            build_view(project)
        reached.append(len(hashed))
    assert 0 < reached[0] < reached[1] < 203
    start[0] = 10**9
    build_view(project)
    assert len(hashed) == 203                           # every file once, over the three attempts: nothing twice


def test_the_memory_of_hashes_survives_two_threads_and_a_damaged_file(project):
    import threading

    kept = plain.read(project)[1] / plain.MEMORY
    stop, bad = threading.Event(), []

    def walk():
        while not stop.is_set():
            plain.read(project)

    def watch():
        while not stop.is_set():
            try:
                json.loads(kept.read_text())
            except ValueError:
                bad.append(1)
            except OSError:
                pass

    threads = [threading.Thread(target=walk) for _ in range(4)] + [threading.Thread(target=watch)]
    for thread in threads:
        thread.start()
    import time
    until, number = time.monotonic() + 1.5, 0
    while time.monotonic() < until:
        number += 1
        (project / "src" / "tokens.py").write_text(f"def refresh_token(store):\n    return {number}\n")
        (project / f"extra{number % 50}.py").write_text(f"x = {number}\n" * 400)
    stop.set()
    for thread in threads:
        thread.join()
    assert bad == []                                    # the file is always whole
    right = dict(build_view(project).files)
    whole = json.loads(kept.read_text())
    walk_time = dict(whole, **{"src/tokens.py": [*whole["src/tokens.py"][:3], "four", whole["src/tokens.py"][4]]})
    blob_id = dict(whole, **{"notes.md": [*whole["notes.md"][:4], 5]})
    for damaged in (json.dumps(walk_time), json.dumps(blob_id), '{"a": [1, 2]}', "[1, 2]", "{"):
        kept.write_text(damaged)
        plain.forget()
        assert build_view(project).files == right       # a memory that cannot be believed is not believed


def test_a_cache_that_cannot_be_written_or_lies_inside_the_directory(project, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CACHE_HOME", str(project / "cache"))
    engine = Engine(project)
    engine.search("refresh token")
    assert not [path for path in build_view(project).files if path.startswith("cache/")]     # Aleph's own cache is not source
    assert engine.status()["pending"] == 0
    if os.geteuid() != 0:
        locked = project.parent / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        monkeypatch.setenv("XDG_CACHE_HOME", str(locked))
        try:
            assert cli.main(["--cwd", str(project), "search", "refresh"]) == 2
            assert "cannot be written" in capsys.readouterr().err
        finally:
            locked.chmod(0o700)


def test_one_directory_of_very_many_entries_is_refused_without_reading_it_through(project, monkeypatch):
    for number in range(30):
        (project / f"n{number}.py").write_text("x = 1\n")
    hashed = []
    real = gitio.hash_blob
    monkeypatch.setattr(gitio, "hash_blob", lambda data, fmt="sha1": hashed.append(1) or real(data, fmt))
    monkeypatch.setattr(plain, "ENTRY_LIMIT", 10)
    with pytest.raises(gitio.NotARepository, match="more than 10 entries"):
        build_view(project)
    assert hashed == []                                 # counted while listing, before anything was read


def test_system_directories_are_not_walked_under_any_of_their_names(project):
    for refused in ("/private/var", "/private/etc", "/var", "/etc", "/usr/local", "/System/Volumes", "/System/Volumes/Data",
                    "/System/Library/Fonts", "/Volumes/SomeDisk", "/Users/somebody", "/home/somebody", "/proc/1", "/opt/tool"):
        assert plain.refusal(Path(refused)), refused
    for allowed in ("/private/tmp/project", "/private/var/folders/z4/abc/T/work/project", "/Users/somebody/project",
                    "/Volumes/Disk/Projects/thing", "/opt/tool/src", "/srv/site/app"):
        assert plain.refusal(Path(allowed)) is None, allowed
    home = Path.home()
    assert plain.refusal(Path("/System/Volumes/Data", *home.parts[1:]))          # the home directory, as the data volume spells it
    assert plain.refusal(Path("/System/Volumes/Data", *home.parent.parts[1:]))


def test_the_walk_never_enters_the_home_directory_or_another_disk(project, tmp_path, monkeypatch):
    """A backstop: whatever path led here, a directory that IS the home directory is not read."""
    inside = project / "elsewhere"
    inside.mkdir()
    (inside / "private.py").write_text("def mine(): ...\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: inside))            # home lies inside what is searched
    monkeypatch.setattr(plain, "refusal", lambda root: None)                      # ... and the rule did not see it
    with pytest.raises(gitio.NotARepository, match="home directory"):
        build_view(project)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    real = os.stat(project).st_dev
    entry_stat = os.DirEntry.stat if hasattr(os.DirEntry, "stat") else None
    assert entry_stat is not None
    monkeypatch.setattr(plain, "_device", lambda entry: real + (1 if entry.name == "elsewhere" else 0))   # as if a disk were mounted there
    assert not [path for path in build_view(project).files if path.startswith("elsewhere/")]


def test_a_link_into_a_plain_directory_from_inside_a_repository_and_a_broken_git_link(project, tmp_path):
    repository = tmp_path / "work" / "repo"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    (repository / "ext").symlink_to(project, target_is_directory=True)
    with pytest.raises(gitio.GitError, match="cannot read the repository"):       # a `.git` is above the path as it was written
        build_view(repository / "ext")
    broken = project / "sub"
    broken.mkdir()
    (broken / ".git").symlink_to(tmp_path / "gone" / "modules" / "sub")           # a submodule whose repository went away
    (broken / "theirs.py").write_text("def theirs(): ...\n")
    for name in ("Node_modules", "VENV"):
        (project / name).mkdir()
        (project / name / "x.py").write_text("x = 1\n")
    view = build_view(project)
    assert sorted(view.files) == ["notes.md", "src/invoice.ts", "src/tokens.py"] and view.inner_repositories == 1


def test_a_file_that_cannot_be_read_is_counted(project):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    locked = project / "src" / "locked.py"
    locked.write_text("def locked(): ...\n")
    locked.chmod(0o000)
    try:
        view = build_view(project)
        assert "src/locked.py" not in view.files and view.left_unread == 1
        assert Engine(project).status()["unreadable"] == 1
    finally:
        locked.chmod(0o600)
