import apsw
import pytest

from aleph_zero.store import SCHEMA_VERSION, StoreError, index_path, open_db, transaction


def test_open_creates_the_schema_in_wal_mode(tmp_path):
    conn = open_db(tmp_path / "nested" / "index.db")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert {"blobs", "chunks", "blob_chunks", "chunk_fts"} <= tables
    assert conn.pragma("journal_mode") == "wal"
    assert conn.pragma("user_version") == SCHEMA_VERSION
    assert conn.pragma("foreign_keys") == 1


def test_reopening_keeps_the_data(tmp_path):
    path = tmp_path / "index.db"
    conn = open_db(path)
    conn.execute("INSERT INTO blobs VALUES ('abc', 'text', 1, 1, 0)")
    conn.close()
    assert open_db(path).execute("SELECT count(*) FROM blobs").fetchall() == [(1,)]


def test_a_newer_schema_is_refused_not_overwritten(tmp_path):
    path = tmp_path / "index.db"
    open_db(path).close()
    raw = apsw.Connection(str(path))
    raw.pragma("user_version", SCHEMA_VERSION + 1)
    raw.close()
    with pytest.raises(StoreError, match="schema version"):
        open_db(path)


def test_transaction_rolls_back_on_error(tmp_path):
    conn = open_db(tmp_path / "index.db")
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO blobs VALUES ('abc', 'text', 1, 1, 0)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM blobs").fetchall() == [(0,)]


def test_index_lives_in_the_directory_all_worktrees_share(repo, tmp_path):
    other = tmp_path / "other"
    repo.git("worktree", "add", "-q", "--detach", str(other))
    assert index_path(other) == index_path(repo.root)
    assert index_path(repo.root).parts[-2:] == ("aleph-zero", "index.db")
