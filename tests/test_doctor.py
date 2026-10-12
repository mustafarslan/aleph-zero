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
    assert code == 0 and "ok    git " in out and "note  not inside a git repository" in out


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
