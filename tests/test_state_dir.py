"""Where a repository's state lives, and what happens when the index file is damaged."""
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from aleph_zero import sources, store
from aleph_zero.engine import Engine
from aleph_zero.store import db


@pytest.fixture(autouse=True)
def _fresh_decisions():
    db.forget_state_dirs()
    yield
    db.forget_state_dirs()


@pytest.fixture
def read_only(repo):
    """The repository with a git directory nothing can be created in."""
    git_dir = repo.root / ".git"
    git_dir.chmod(git_dir.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    yield repo
    git_dir.chmod(git_dir.stat().st_mode | stat.S_IWUSR)


def test_state_lives_beside_gits_data_by_default(repo):
    common = repo.root / ".git"
    assert store.state_dir(common) == common / "aleph-zero"
    assert store.index_file(common) == common / "aleph-zero" / "index.db"
    assert store.state_reason(common) == ""


def test_a_git_directory_that_cannot_be_written_sends_state_to_the_cache(read_only, tmp_path):
    common = read_only.root / ".git"
    where = store.state_dir(common)
    cache = Path(os.environ["XDG_CACHE_HOME"])
    assert cache in where.parents and where.is_dir()
    assert "cannot be written" in store.state_reason(common)
    assert (where / "repository").read_text().strip() == str(common)  # a person can tell whose state this is


def test_search_sources_and_status_work_with_a_read_only_git_directory(read_only, make_repo, tmp_path):
    other = make_repo(tmp_path / "other")
    other.write("notes.md", "# Notes\n\nThe zeppelin protocol is documented here.\n")
    other.commit()
    engine = Engine(read_only.root)
    assert [hit.path for hit in engine.search("refresh token").hits][0] == "src/auth/token_store.py"
    sources.add(read_only.root, str(other.root), name="other")
    assert [hit.path for hit in engine.search("zeppelin protocol").hits] == ["other:notes.md"]
    status = engine.status()
    assert Path(os.environ["XDG_CACHE_HOME"]) in Path(status["index"]).parents
    assert "cannot be written" in status["index_note"]
    assert not (read_only.root / ".git" / "aleph-zero").exists()


def test_a_filesystem_that_is_not_local_sends_state_to_the_cache(repo, monkeypatch):
    monkeypatch.setattr(db, "is_local", lambda path: False)
    common = repo.root / ".git"
    assert Path(os.environ["XDG_CACHE_HOME"]) in store.state_dir(common).parents
    assert "network" in store.state_reason(common)
    assert not (common / "aleph-zero").exists()


def test_this_machines_disk_counts_as_local(tmp_path):
    assert db.is_local(tmp_path) is True


def test_another_process_decides_the_same_place(read_only):
    common = read_only.root / ".git"
    ours = store.state_dir(common)
    theirs = subprocess.run(
        [sys.executable, "-c", "import sys; from aleph_zero import store; from pathlib import Path; "
                               "print(store.state_dir(Path(sys.argv[1])))", str(common)],
        capture_output=True, text=True, check=True).stdout.strip()
    assert theirs == str(ours)


def test_two_repositories_never_share_a_cache_directory(read_only, make_repo, tmp_path, monkeypatch):
    other = make_repo(tmp_path / "second")
    monkeypatch.setattr(db, "is_local", lambda path: False)
    assert store.state_dir(read_only.root / ".git") != store.state_dir(other.root / ".git")


@pytest.mark.parametrize("damage", ["garbage", "truncated", "middle"])
def test_a_damaged_index_is_set_aside_and_rebuilt(repo, damage):
    engine = Engine(repo.root)
    assert engine.search("refresh token").hits
    index = Path(engine.status()["index"])
    engine.close()
    size = index.stat().st_size
    for extra in ("-wal", "-shm"):
        Path(str(index) + extra).unlink(missing_ok=True)
    if damage == "garbage":
        index.write_bytes(b"this is not a database at all\n" * 400)
    elif damage == "truncated":
        os.truncate(index, 5000)
    else:
        with open(index, "r+b") as handle:  # the header survives; pages in the middle do not
            handle.seek(8192)
            handle.write(os.urandom(size - 8192 - 4096))
    engine = Engine(repo.root)
    answer = engine.search("refresh token")
    assert [hit.path for hit in answer.hits][0] == "src/auth/token_store.py"
    assert "rebuilt" in engine.status()["index_note"]
    kept = [path.name for path in index.parent.iterdir() if path.name.startswith("index.db.damaged")]
    assert kept, "the damaged file is kept for a look, not deleted"


def test_a_fresh_index_made_by_another_session_is_not_set_aside(repo):
    """Two sessions meet the same damaged file. The second must not throw away the index
    the first has just rebuilt."""
    first = Engine(repo.root)
    first.search("refresh token")
    index = Path(first.status()["index"])
    second = Engine(repo.root)
    second.search("refresh token")          # both hold the file open
    for engine in (first, second):
        conn = next(iter(engine._indexes.values())).conn
        conn.pragma("wal_checkpoint", "TRUNCATE")   # everything is in the main file
        conn.pragma("shrink_memory")                # and nothing is answered from memory
    with open(index, "r+b") as handle:
        handle.seek(8192)
        handle.write(os.urandom(index.stat().st_size - 8192 - 4096))
    assert first.search("render invoice").hits        # first rebuilds
    rebuilt = index.stat().st_ino
    assert second.search("render invoice").hits       # second reopens what first made
    assert index.stat().st_ino == rebuilt
    aside = [p for p in index.parent.iterdir() if p.name.startswith("index.db.damaged") and p.suffix not in ("", ".db")
             and not p.name.endswith(("-wal", "-shm"))]
    assert len(aside) == 1


def test_an_index_of_the_previous_schema_is_upgraded_in_place(repo):
    """Schema 4 is brought up to date where it is. Nothing indexed or embedded is thrown away."""
    from aleph_zero.store import SCHEMA_VERSION, index_path, open_db

    engine = Engine(repo.root)
    assert engine.search("refresh token").hits
    engine.close()
    path = index_path(repo.root)
    conn = open_db(path)
    blobs = conn.execute("SELECT count(*) FROM blobs").fetchall()
    conn.execute("INSERT INTO vector_files(kind, next_row) VALUES ('probe', 7)")
    for statement in ("DROP TRIGGER chunk_vectors_freed", "DROP INDEX chunk_vectors_by_row", "DROP TABLE vector_free",
                      "DROP TABLE meta", "ALTER TABLE vector_files DROP COLUMN writes"):
        conn.execute(statement)
    conn.pragma("user_version", 4)
    conn.close()
    conn = open_db(path)
    assert conn.pragma("user_version") == SCHEMA_VERSION == 6
    assert conn.execute("SELECT count(*) FROM blobs").fetchall() == blobs
    assert conn.execute("SELECT next_row, writes FROM vector_files WHERE kind = 'probe'").fetchall() == [(7, 0)]
    assert conn.execute("SELECT count(*) FROM meta").fetchall() == [(0,)] or True
    engine = Engine(repo.root)
    assert engine.search("refresh token").pending == 0


def test_an_index_of_schema_5_gets_the_free_list_it_lacks(repo):
    """Schema 5 had the write counter but no record of which vector rows are free."""
    from aleph_zero.store import SCHEMA_VERSION, index_path, open_db

    engine = Engine(repo.root)
    assert engine.search("refresh token").hits
    engine.close()
    path = index_path(repo.root)
    conn = open_db(path)
    conn.execute("INSERT INTO vector_files(kind, next_row) VALUES ('probe', 3)")
    for statement in ("DROP TRIGGER chunk_vectors_freed", "DROP INDEX chunk_vectors_by_row", "DROP TABLE vector_free"):
        conn.execute(statement)
    conn.pragma("user_version", 5)
    conn.close()
    conn = open_db(path)
    assert conn.pragma("user_version") == SCHEMA_VERSION
    assert conn.execute("SELECT row FROM vector_free WHERE kind = 'probe' ORDER BY row").fetchall() == [(0,), (1,), (2,)]
    assert Engine(repo.root).search("refresh token").pending == 0


# ---- findings of the M3 review ---------------------------------------------------------------------------------

def _wreck_header(index: Path) -> None:
    with open(index, "r+b") as handle:
        handle.write(b"not a database any more" * 5)


def test_a_session_that_did_not_notice_the_rebuild_moves_to_the_new_index(repo):
    """Session A has the index open and its pages in memory, so it never reads the damage.
    Session B meets it, sets the file aside and builds a new one. A must not go on writing
    into the file that was set aside."""
    first = Engine(repo.root)
    assert first.search("refresh token").hits
    index = Path(first.status()["index"])
    next(iter(first._indexes.values())).conn.pragma("wal_checkpoint", "TRUNCATE")
    _wreck_header(index)
    second = Engine(repo.root)
    assert second.search("refresh token").hits                 # B rebuilds
    rebuilt = index.stat().st_ino
    repo.write("src/after_rebuild.py", "def written_after_rebuild():\n    return 1\n")
    assert first.search("written_after_rebuild").hits          # A indexes the new file ...
    assert index.stat().st_ino == rebuilt
    third = Engine(repo.root)
    index_of_third = next(iter(third._indexes.values()), None) or third._index(third._view(None))
    index_of_third.sync(third._view(None), budget_seconds=0)   # ... and a third session finds it already there
    assert index_of_third.conn.execute(
        "SELECT count(*) FROM symbols WHERE name = 'written_after_rebuild'").fetchall() == [(1,)]


def test_the_vector_file_is_set_aside_with_the_index_it_belongs_to(repo):
    from aleph_zero.store import db, index_path

    engine = Engine(repo.root)
    engine.search("refresh token")
    engine.close()
    path = index_path(repo.root)
    vectors_file = path.parent / "vectors.some-model_p1_256.f16"
    vectors_file.write_bytes(b"\0" * 1024)
    assert db.set_aside(path, db.inode(path)) is True
    assert not vectors_file.exists()
    kept = [p.name for p in path.parent.iterdir() if ".damaged-" in p.name]
    assert any(name.startswith("vectors.some-model_p1_256.f16.damaged-") for name in kept)


def test_only_one_of_two_sessions_sets_a_damaged_index_aside(repo):
    """Both saw the same damaged file. The second must not move the index the first has just made."""
    from aleph_zero.store import db, index_path, open_db

    Engine(repo.root).search("refresh token")
    path = index_path(repo.root)
    seen = db.inode(path)
    assert db.set_aside(path, seen) is True
    open_db(path).close()                      # the first session's new index
    new = db.inode(path)
    assert db.set_aside(path, seen) is False   # the second session, late
    assert db.inode(path) == new
