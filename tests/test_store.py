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
    conn.execute("INSERT INTO blobs VALUES ('abc', 'text', 'text', 1, 1, 0)")
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
            conn.execute("INSERT INTO blobs VALUES ('abc', 'text', 'text', 1, 1, 0)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM blobs").fetchall() == [(0,)]


def test_index_lives_in_the_directory_all_worktrees_share(repo, tmp_path):
    other = tmp_path / "other"
    repo.git("worktree", "add", "-q", "--detach", str(other))
    assert index_path(other) == index_path(repo.root)
    assert index_path(repo.root).parts[-2:] == ("aleph-zero", "index.db")


def test_an_index_from_an_older_version_is_rebuilt_because_it_is_a_cache(tmp_path):
    path = tmp_path / "index.db"
    conn = open_db(path)
    conn.execute("INSERT INTO blobs(blob_oid, family, kind, size, chunker_version, last_seen_at) "
                 "VALUES ('a', 'text', 'text', 1, 1, 0)")
    conn.execute("INSERT INTO chunk_fts(rowid, symbol, signature, doc, body) VALUES (1, 'a', 'b', 'c', 'd')")
    conn.close()
    raw = apsw.Connection(str(path))
    raw.pragma("user_version", 3)  # older than anything that is upgraded in place (4 onwards is: test_state_dir)
    raw.close()
    conn = open_db(path)
    assert conn.pragma("user_version") == SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM blobs").fetchall() == [(0,)]
    assert conn.execute("SELECT count(*) FROM chunk_fts").fetchall() == [(0,)]


def test_an_index_of_schema_9_gains_the_table_of_record_vectors_and_keeps_what_it_has(tmp_path):
    path = tmp_path / "index.db"
    conn = open_db(path)
    conn.execute("INSERT INTO blobs VALUES ('abc', 'text', 'text', 1, 1, 0)")
    conn.execute("DROP TABLE record_vectors")
    conn.pragma("user_version", 9)
    conn.close()
    conn = open_db(path)
    assert conn.pragma("user_version") == SCHEMA_VERSION == 10
    assert conn.execute("SELECT count(*) FROM record_vectors").fetchall() == [(0,)]
    assert conn.execute("SELECT count(*) FROM blobs").fetchall() == [(1,)]


def test_threads_that_first_ask_where_a_repository_s_state_lives_all_get_the_same_answer(repo):
    """Found by a test of something else (2026-10-13): the probe that says whether the directory beside git's data can
    be written had one name for the whole process. Two threads probing at once removed each other's file, the
    second took that for "cannot be written" and put its state in the cache directory: one session, two indexes.
    A server has two threads that ask this first: the one that answers calls and the one that embeds."""
    import threading

    from aleph_zero import gitio
    from aleph_zero.store import db

    common = gitio.common_dir(repo.root)
    beside = common / "aleph-zero"
    for _ in range(40):
        db.forget_state_dirs()
        start = threading.Barrier(8)
        answers = []

        def ask():
            start.wait()
            answers.append((db.state_dir(common), db.state_reason(common)))

        threads = [threading.Thread(target=ask) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert set(answers) == {(beside, "")}
    db.forget_state_dirs()


def test_a_rewrite_of_the_temporary_tables_that_is_interrupted_says_interrupted(tmp_path):
    """Found 2026-10-13 by asking the engine as the plugin's second rung asks it (a kind filter and 400 ms) on a large
    repository. When SQLite interrupts a write inside a transaction it rolls the whole transaction back, the savepoint
    with it; rolling back to that savepoint then failed with "no such savepoint", and that error, not the
    interruption, went up through a search that should have answered "out of time"."""
    from aleph_zero import indexer

    conn = open_db(tmp_path / "index.db")
    with pytest.raises(apsw.InterruptError):
        with indexer._temp_tables(conn):
            conn.execute("DELETE FROM temp.scope_chunks")
            conn.execute("ROLLBACK")                      # what SQLite has done by itself when it interrupts
            raise apsw.InterruptError("interrupted")
    with indexer._temp_tables(conn):                      # and the connection is usable afterwards
        conn.execute("DELETE FROM temp.scope_chunks")
    with pytest.raises(RuntimeError):                     # anything else still undoes the rewrite and goes up as it is
        with indexer._temp_tables(conn):
            conn.execute("INSERT INTO temp.scope_chunks(chunk_id) VALUES (7)")
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM temp.scope_chunks").fetchall() == [(0,)]
