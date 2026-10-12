from aleph_zero import cli, doctor
from aleph_zero.lang import parse


def run(capsys, *args):
    code = cli.main(list(args))
    return code, capsys.readouterr().out


def test_a_healthy_setup_passes_and_says_what_it_checked(repo, capsys):
    cli.main(["--cwd", str(repo.root), "index"])
    capsys.readouterr()
    code, out = run(capsys, "--cwd", str(repo.root), "doctor")
    assert code == 0
    for expected in ("ok    git ", "ok    SQLite ", "ok    tree-sitter ", "ok    grammars: 9 of 9", "ok    index: "):
        assert expected in out, expected
    assert "note  git's untracked cache is off" in out and "git config core.untrackedCache true" in out
    assert "FAIL" not in out


def test_the_untracked_cache_is_reported_when_on(repo, capsys):
    repo.git("config", "core.untrackedCache", "true")
    code, out = run(capsys, "--cwd", str(repo.root), "doctor")
    assert code == 0 and "ok    git's untracked cache is on" in out


def test_a_damaged_index_fails_the_check(repo, capsys):
    cli.main(["--cwd", str(repo.root), "index"])
    capsys.readouterr()
    index = repo.root / ".git" / "aleph-zero" / "index.db"
    for extra in (index.with_name("index.db-wal"), index.with_name("index.db-shm")):
        extra.unlink(missing_ok=True)
    index.write_bytes(b"this is not a database" * 100)
    code, out = run(capsys, "--cwd", str(repo.root), "doctor")
    assert code == 1 and "FAIL  index:" in out and "sets it aside" in out


def test_outside_a_repository_the_tools_are_still_checked(tmp_path, capsys):
    code, out = run(capsys, "--cwd", str(tmp_path), "doctor")
    assert code == 0 and "ok    git " in out and "is not a git repository: its 0 files are searched as they are on disk" in out


def test_a_missing_grammar_fails_and_fetching_is_offered(repo, capsys, monkeypatch):
    real = parse.language

    def without_rust(profile):
        if profile.grammar == "rust":
            raise LookupError("language not found: rust")
        return real(profile)

    monkeypatch.setattr(parse, "language", without_rust)
    code, out = run(capsys, "--cwd", str(repo.root), "doctor")
    assert code == 1 and "FAIL  grammars: 8 of 9" in out and "rust" in out and "--fetch-grammars" in out


def test_fetching_grammars_is_one_flag(repo, capsys, monkeypatch):
    fetched = []
    monkeypatch.setattr(parse, "ensure_grammars", lambda: fetched.append(True) or 9)
    code, out = run(capsys, "--cwd", str(repo.root), "doctor", "--fetch-grammars")
    assert code == 0 and fetched == [True] and "fetched grammars" in out


def test_the_report_is_also_available_as_data(repo):
    report = doctor.check(repo.root)
    assert {row.level for row in report} <= {"ok", "note", "FAIL"} and any(row.name == "git" for row in report)


def test_doctor_says_when_the_index_is_not_beside_gits_data(repo, monkeypatch):
    from aleph_zero.store import db

    db.forget_state_dirs()
    monkeypatch.setattr(db, "is_local", lambda path: False)
    try:
        rows = {row.name: row for row in doctor.check(repo.root)}
    finally:
        db.forget_state_dirs()
    assert rows["index location"].level == "note" and "network" in rows["index location"].detail


def test_doctor_describes_the_vector_file(repo, fake_worker, monkeypatch, capsys):
    import numpy as np

    from aleph_zero.indexer import Index
    from aleph_zero.store import index_path, open_db
    from aleph_zero.vectors import WIDTH, VectorStore
    from aleph_zero.view import build_view

    index = Index(open_db(index_path(repo.root)))
    index.sync(build_view(repo.root))
    ids = [row[0] for row in index.conn.execute("SELECT chunk_id FROM temp.view_chunks ORDER BY chunk_id")][:2]
    rows = np.ones((2, WIDTH), dtype=np.float32) / np.sqrt(WIDTH)
    VectorStore(index.conn, index_path(repo.root).parent, "kind/p1/256").add(ids, rows)
    index.conn.close()
    code, out = run(capsys, "--cwd", str(repo.root), "doctor")
    assert "2 rows in use, 0 free to be used again" in out


def test_the_untracked_cache_switched_on_in_the_index_alone_is_seen_and_offered(repo, capsys):
    """`git update-index --untracked-cache` keeps the cache in this repository's index and changes no configuration."""
    from aleph_zero import gitio

    code, out = run(capsys, "--cwd", str(repo.root), "doctor")
    assert "git update-index --untracked-cache" in out and "no configuration" in out      # the way that changes none
    assert gitio.untracked_cache_enabled(repo.root) is False
    repo.git("update-index", "--untracked-cache")
    repo.git("status")
    import subprocess
    assert subprocess.run(["git", "-C", str(repo.root), "config", "--get", "core.untrackedCache"], capture_output=True).returncode != 0   # nothing was configured
    assert gitio.untracked_cache_enabled(repo.root) is True
    code, out = run(capsys, "--cwd", str(repo.root), "doctor")
    assert "ok    git's untracked cache is on" in out
    repo.git("update-index", "--no-untracked-cache")
    assert gitio.untracked_cache_enabled(repo.root) is False


def test_doctor_says_how_much_of_the_vector_file_is_on_disk(tmp_path):
    """After free rows' blocks were given back the file is as long as before and smaller on disk: both are said."""
    from aleph_zero import doctor

    index = tmp_path / "index.db"
    index.write_bytes(b"")
    file = tmp_path / "vectors.test.f16"
    with open(file, "wb") as handle:
        handle.truncate(8 * 1024 * 1024)             # eight megabytes long, nothing of it on disk
    said = doctor._vector_file(index)
    assert "vectors 8.4 MB long, 0.0 MB on disk" in said
    file.write_bytes(b"x" * 1024 * 1024)             # all of it on disk: said once, as before
    assert "vectors 1.0 MB" in doctor._vector_file(index) and "on disk" not in doctor._vector_file(index)


def test_an_extension_of_a_git_index_is_found_where_extensions_are_and_not_by_its_letters_alone():
    from aleph_zero import gitio

    def extension(name: bytes, body: bytes) -> bytes:
        return name + len(body).to_bytes(4, "big") + body

    entries = b"DIRC" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big") + b"\x00" * 70
    assert gitio._has_extension(entries + extension(b"UNTR", b"cache") + b"\x11" * 20, b"UNTR") is True
    assert gitio._has_extension(entries + extension(b"TREE", b"t" * 9) + extension(b"UNTR", b"") + b"\x11" * 20, b"UNTR") is True
    assert gitio._has_extension(entries + extension(b"UNTR", b"x") + extension(b"REUC", b"yy") + b"\x22" * 32, b"UNTR") is True   # a sha256 index
    assert gitio._has_extension(entries + extension(b"TREE", b"t" * 9) + b"\x11" * 20, b"UNTR") is False
    by_chance = b"DIRC" + b"\x00" * 30 + b"UNTR" + b"\x00\x00\x00\x03" + b"abc" + b"\x00" * 40      # in a path of an entry: leads nowhere
    assert gitio._has_extension(by_chance + extension(b"TREE", b"t") + b"\x11" * 20, b"UNTR") is False
    assert gitio._has_extension(b"", b"UNTR") is False and gitio._has_extension(b"UNTR", b"UNTR") is False
