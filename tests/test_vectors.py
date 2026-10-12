import multiprocessing
import time

import numpy as np

from aleph_zero import vectorfile, vectors
from aleph_zero.indexer import Index, collect_garbage
from aleph_zero.store import index_path, open_db
from aleph_zero.vectors import VectorStore
from aleph_zero.view import build_view

KIND = "test-model/p1/256"


def unit(seed, count=1):
    rng = np.random.default_rng(seed)
    rows = rng.standard_normal((count, vectors.WIDTH)).astype(np.float32)
    return rows / np.linalg.norm(rows, axis=1, keepdims=True)


def ready(repo):
    index = Index(open_db(index_path(repo.root)))
    index.sync(build_view(repo.root))
    chunk_ids = [row[0] for row in index.conn.execute("SELECT chunk_id FROM temp.view_chunks ORDER BY chunk_id")]
    return index, VectorStore(index.conn, index_path(repo.root).parent, KIND), chunk_ids


def test_stored_vectors_come_back_for_their_chunks_and_the_nearest_is_found(repo):
    index, store, chunk_ids = ready(repo)
    matrix = unit(1, len(chunk_ids))
    assert store.add(chunk_ids, matrix) == len(chunk_ids)
    assert store.missing(index) == []
    for position in (0, len(chunk_ids) - 1):
        best = store.search(index, matrix[position], limit=3)
        assert best[0][0] == chunk_ids[position] and best[0][1] > 0.99
        assert best[0][1] > best[1][1]
    assert store.coverage(index) == (len(chunk_ids), len(chunk_ids))


def test_chunks_without_a_vector_are_listed_and_adding_twice_stores_once(repo):
    index, store, chunk_ids = ready(repo)
    assert store.missing(index) == chunk_ids and store.search(index, unit(2)[0], limit=5) == []
    assert store.add(chunk_ids[:2], unit(3, 2)) == 2
    assert store.missing(index) == chunk_ids[2:] and store.coverage(index) == (2, len(chunk_ids))
    assert store.add(chunk_ids[:3], unit(4, 3)) == 1   # two already had one: another session got there first
    rows = index.conn.execute("SELECT count(*), count(DISTINCT row) FROM chunk_vectors").fetchall()
    assert rows == [(3, 3)]


def test_only_chunks_of_the_view_are_searched(repo):
    index, store, chunk_ids = ready(repo)
    matrix = unit(5, len(chunk_ids))
    store.add(chunk_ids, matrix)
    repo.write("src/billing/invoice.py", "def replaced():\n    return 1\n")
    index.sync(build_view(repo.root))
    now_in_view = {row[0] for row in index.conn.execute("SELECT chunk_id FROM temp.view_chunks")}
    gone = next(position for position, chunk_id in enumerate(chunk_ids) if chunk_id not in now_in_view)
    found = [chunk_id for chunk_id, _ in store.search(index, matrix[gone], limit=50)]
    assert chunk_ids[gone] not in found and set(found) <= now_in_view


def test_vectors_of_another_model_or_prefix_are_not_mixed_in(repo):
    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids, unit(6, len(chunk_ids)))
    other = VectorStore(index.conn, index_path(repo.root).parent, "other-model/p1/256")
    assert other.missing(index) == chunk_ids and other.search(index, unit(6)[0], limit=5) == []
    assert other.path != store.path


def _add_in_child(root, kind, queue):
    from pathlib import Path

    index = Index(open_db(index_path(Path(root))))
    index.sync(build_view(Path(root)))
    store = VectorStore(index.conn, index_path(Path(root)).parent, kind)
    chunk_ids = store.missing(index)
    queue.put(store.add(chunk_ids, unit(7, len(chunk_ids))))


def test_vectors_written_by_another_process_are_seen_by_one_that_already_mapped_the_file(repo):
    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids[:1], unit(7, len(chunk_ids))[:1])
    assert len(store.search(index, unit(7, len(chunk_ids))[0], limit=10)) == 1   # the file is now mapped here
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=_add_in_child, args=(str(repo.root), KIND, queue))
    child.start()
    assert queue.get(timeout=60) == len(chunk_ids) - 1
    child.join()
    assert len(store.search(index, unit(8)[0], limit=50)) == len(chunk_ids)
    assert store.missing(index) == []


def test_garbage_collection_drops_the_row_table_entry_and_frees_its_row(repo):
    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids, unit(9, len(chunk_ids)))
    highest = index.conn.execute("SELECT max(row) FROM chunk_vectors").fetchall()[0][0]
    repo.write("src/billing/invoice.py", "def replaced():\n    return 1\n")
    repo.commit("replace")
    view = build_view(repo.root)
    index.sync(view)
    collect_garbage(index.conn, max_age_days=0, now=time.time() + 10)
    kept = index.conn.execute("SELECT count(*) FROM chunk_vectors").fetchall()[0][0]
    assert kept < len(chunk_ids)                                   # the vector went with its chunk
    assert index.conn.execute("SELECT count(*) FROM chunk_vectors WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)").fetchall() == [(0,)]
    new = store.missing(index)
    store.add(new, unit(10, len(new)))
    rows = [row[0] for row in index.conn.execute("SELECT row FROM chunk_vectors WHERE chunk_id IN (%s)" % ",".join("?" * len(new)), new)]
    # The freed row is used again: nobody reads a row whose chunk is gone, and the file must
    # not grow for ever. (Until M3 a row was never reused.)
    assert min(rows) <= highest
    assert index.conn.execute("SELECT count(*), count(DISTINCT row) FROM chunk_vectors").fetchall()[0] == (kept + len(new),) * 2


def test_a_file_shorter_than_the_row_table_loses_those_rows_and_they_are_embedded_again(repo):
    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids, unit(11, len(chunk_ids)))
    row_bytes = vectors.WIDTH * 2
    with open(store.path, "r+b") as handle:
        handle.truncate(row_bytes * 2)   # as after a crash of the machine: the database kept more than the file
    reopened = VectorStore(index.conn, index_path(repo.root).parent, KIND)
    assert len(reopened.missing(index)) == len(chunk_ids) - 2
    assert len(reopened.search(index, unit(11)[0], limit=50)) == 2


def test_three_hundred_thousand_vectors_are_scanned_quickly(tmp_path):
    conn = open_db(tmp_path / "aleph-zero" / "index.db")
    count = 300_000
    store = VectorStore(conn, tmp_path / "aleph-zero", KIND)
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO chunks(chunk_id, chunk_hash, kind, symbol, signature, breadcrumb, text) VALUES (?, ?, 'function', '', '', '', '')",
                     ((i, f"h{i}") for i in range(1, count + 1)))
    conn.execute("INSERT INTO temp.view_chunks(chunk_id) SELECT chunk_id FROM chunks")
    conn.execute("COMMIT")
    matrix = unit(12, 4096)
    for start in range(1, count + 1, 4096):
        ids = list(range(start, min(start + 4096, count + 1)))
        store.add(ids, matrix[: len(ids)])
    index = Index(conn)
    index._view_epoch = 1
    store.search(index, matrix[0], limit=10)
    started = time.perf_counter()
    found = store.search(index, matrix[5], limit=10)
    assert time.perf_counter() - started < 0.03 and len(found) == 10 and found[0][1] > 0.99


def test_files_with_no_language_get_no_vectors_and_are_not_counted_as_missing(repo):
    repo.write("fixtures/data.json", "".join(f'{{"key_{i}": "value_{i}"}}\n' for i in range(120)))
    repo.write("config/settings.cfg", "[section]\noption = bettongword\n")
    index, store, chunk_ids = ready(repo)
    kinds = dict(index.conn.execute(
        "SELECT c.chunk_id, c.kind FROM chunks c JOIN temp.view_chunks v ON v.chunk_id = c.chunk_id"))
    blocks = [chunk_id for chunk_id in chunk_ids if kinds[chunk_id] == "block"]
    assert len(blocks) >= 4
    wanted = store.missing(index)
    assert wanted and not set(wanted) & set(blocks)          # data files are searched by their words only
    assert store.coverage(index) == (0, len(chunk_ids) - len(blocks))
    store.add(wanted, unit(21, len(wanted)))
    assert store.missing(index) == [] and store.coverage(index) == (len(wanted), len(wanted))


def test_a_vector_is_not_stored_for_a_chunk_id_that_now_holds_other_content(repo):
    index, store, chunk_ids = ready(repo)
    hashes = dict(index.conn.execute("SELECT chunk_id, chunk_hash FROM chunks"))
    claimed = [hashes[chunk_ids[0]], "the hash of what this id held when its text was read"]
    assert store.add(chunk_ids[:2], unit(31, 2), hashes=claimed) == 1   # the second id was collected and reissued meanwhile
    assert [row[0] for row in index.conn.execute("SELECT chunk_id FROM chunk_vectors")] == [chunk_ids[0]]


# ---- rows of collected chunks are used again (M3) ------------------------------------------------------

def _collect_everything_but_the_view(index):
    return collect_garbage(index.conn, max_age_days=0, now=time.time() + 10)


def test_the_row_of_a_collected_chunk_is_used_again_and_the_file_does_not_grow(repo):
    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids, unit(20, len(chunk_ids)))
    size = store.path.stat().st_size
    rows_before = dict(index.conn.execute("SELECT chunk_id, row FROM chunk_vectors"))
    repo.write("src/billing/invoice.py", "def replaced_one():\n    return 1\n\n\ndef replaced_two():\n    return 2\n")
    index.sync(build_view(repo.root))
    removed = _collect_everything_but_the_view(index)["chunks_removed"]
    assert removed >= 1
    freed = sorted(row[0] for row in index.conn.execute("SELECT row FROM vector_free"))
    assert len(freed) == removed and set(freed) <= set(rows_before.values())
    new = store.missing(index)
    assert len(new) == 2
    store.add(new, unit(21, 2))
    taken = sorted(row[0] for row in index.conn.execute(
        "SELECT row FROM chunk_vectors WHERE chunk_id IN (?, ?)", new))
    assert taken[0] == freed[0]                               # the freed row first
    assert store.path.stat().st_size <= size + vectors.ROW_BYTES * max(0, 2 - removed)
    counted = index.conn.execute("SELECT count(*), count(DISTINCT row) FROM chunk_vectors").fetchall()[0]
    assert counted[0] == counted[1]


def test_two_chunks_can_never_be_given_the_same_row(repo):
    import apsw
    import pytest

    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids[:1], unit(22, 1))
    with pytest.raises(apsw.ConstraintError):
        index.conn.execute("INSERT INTO chunk_vectors(chunk_id, kind, row) VALUES (?, ?, 0)", (chunk_ids[1], KIND))


def test_a_reader_with_a_remembered_row_list_sees_a_vector_written_into_a_used_again_row(repo):
    """The reader's list of rows is remembered between queries. A row that was freed and
    given to a new chunk must be read as the new chunk's, not skipped and not misattributed."""
    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids, unit(23, len(chunk_ids)))
    reader_index = Index(open_db(index_path(repo.root)))
    reader = VectorStore(reader_index.conn, index_path(repo.root).parent, KIND)
    reader_index.sync(build_view(repo.root))
    assert len(reader.search(reader_index, unit(23, len(chunk_ids))[0], limit=50)) == len(chunk_ids)
    repo.write("src/billing/invoice.py", "def replaced_one():\n    return 1\n")
    view = build_view(repo.root)
    index.sync(view)
    _collect_everything_but_the_view(index)
    new = store.missing(index)
    fresh = unit(24, len(new))
    store.add(new, fresh)
    reader_index.sync(view)
    best = reader.search(reader_index, fresh[0], limit=3)
    assert best[0][0] == new[0] and best[0][1] > 0.99
    assert reader.coverage(reader_index) == store.coverage(index)


def test_free_rows_are_listed_for_an_index_upgraded_from_the_previous_schema(repo):
    """Before schema 5 nothing recorded which rows were dead. The upgrade works it out."""
    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids, unit(25, len(chunk_ids)))
    conn = index.conn
    dead = conn.execute("SELECT row FROM chunk_vectors ORDER BY row LIMIT 1").fetchall()[0][0]
    conn.execute("DELETE FROM chunk_vectors WHERE row = ?", (dead,))
    conn.execute("DROP TRIGGER chunk_vectors_freed")
    conn.execute("DROP INDEX chunk_vectors_by_row")
    conn.execute("DROP TABLE vector_free")
    conn.execute("DROP TABLE meta")
    conn.execute("ALTER TABLE vector_files DROP COLUMN writes")
    conn.pragma("user_version", 4)
    conn.close()
    upgraded = open_db(index_path(repo.root))
    assert upgraded.execute("SELECT kind, row FROM vector_free").fetchall() == [(KIND, dead)]
    assert upgraded.execute("SELECT count(*) FROM chunk_vectors").fetchall() == [(len(chunk_ids) - 1,)]


def test_vectors_survive_when_a_newer_chunker_processes_the_same_content_again(repo):
    """Blobs written by an older chunker are processed again. Chunks that come out the same
    keep their ids, so their vectors are still theirs: nothing is embedded twice."""
    index, store, chunk_ids = ready(repo)
    matrix = unit(30, len(chunk_ids))
    store.add(chunk_ids, matrix)
    rows = dict(index.conn.execute("SELECT chunk_id, row FROM chunk_vectors"))
    # As an older build left it: seen from this session as a change made by another one.
    open_db(index_path(repo.root)).execute("UPDATE blobs SET chunker_version = chunker_version - 1")
    view = build_view(repo.root)
    assert index.sync(view).indexed == 3
    assert dict(index.conn.execute("SELECT chunk_id, row FROM chunk_vectors")) == rows
    assert store.missing(index) == [] and store.coverage(index) == (len(chunk_ids), len(chunk_ids))
    assert store.search(index, matrix[0], limit=1)[0][0] == chunk_ids[0]
    assert index.conn.execute("SELECT count(*) FROM vector_free").fetchall() == [(0,)]


def test_a_vector_file_cut_short_leaves_the_row_accounts_in_step_with_the_file(repo):
    """The file lost its tail (restored from an older copy, cut by a full disk). Rows it no
    longer holds are forgotten, and they must not be handed out as free rows: every row,
    in use or free, is a row the file has."""
    import os

    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids, unit(40, len(chunk_ids)))
    kept = 2
    os.truncate(store.path, kept * vectors.ROW_BYTES)
    again = VectorStore(index.conn, index_path(repo.root).parent, KIND)
    one = lambda sql: index.conn.execute(sql).fetchall()[0][0]  # noqa: E731
    assert one("SELECT count(*) FROM chunk_vectors") == kept
    assert one("SELECT next_row FROM vector_files") == kept
    assert one("SELECT count(*) FROM vector_free") == 0
    missing = again.missing(index)
    assert len(missing) == len(chunk_ids) - kept
    again.add(missing, unit(41, len(missing)))
    assert again.path.stat().st_size == len(chunk_ids) * vectors.ROW_BYTES == one("SELECT next_row FROM vector_files") * vectors.ROW_BYTES
    assert again.coverage(index) == (len(chunk_ids), len(chunk_ids))


def test_a_deleted_vector_file_starts_again_from_nothing(repo):
    index, store, chunk_ids = ready(repo)
    store.add(chunk_ids, unit(42, len(chunk_ids)))
    store.path.unlink()
    again = VectorStore(index.conn, index_path(repo.root).parent, KIND)
    one = lambda sql: index.conn.execute(sql).fetchall()[0][0]  # noqa: E731
    assert (one("SELECT count(*) FROM chunk_vectors"), one("SELECT next_row FROM vector_files"),
            one("SELECT count(*) FROM vector_free")) == (0, 0, 0)
    again.add(chunk_ids[:1], unit(43, 1))
    assert again.path.stat().st_size == vectors.ROW_BYTES


# ---- findings of the M3 review ------------------------------------------------------------------------------

def test_a_session_reads_the_vector_file_that_is_there_now_not_one_that_was_deleted(repo):
    """The file is deleted and made again with other vectors (what the deferred list tells
    the owner to do to shrink it). A session that had the old file mapped must not go on
    ranking by bytes that are no longer on disk."""
    index, store, chunk_ids = ready(repo)
    old = unit(50, len(chunk_ids))
    store.add(chunk_ids, old)
    live_index = Index(open_db(index_path(repo.root)))
    live_index.sync(build_view(repo.root))
    live = VectorStore(live_index.conn, index_path(repo.root).parent, KIND)
    assert live.search(live_index, old[3], limit=1)[0][0] == chunk_ids[3]      # the old file is mapped now
    store.path.unlink()
    rebuilt = VectorStore(index.conn, index_path(repo.root).parent, KIND)
    new = unit(51, len(chunk_ids))
    rebuilt.add(chunk_ids, new)
    best = live.search(live_index, new[3], limit=1)[0]
    assert best[0] == chunk_ids[3] and best[1] > 0.99
    assert live.search(live_index, old[3], limit=1)[0][1] < 0.5              # the old vectors are gone


def test_nothing_is_written_through_a_connection_to_an_index_that_was_set_aside(repo):
    """The index file was replaced while this session had it open. Its row numbers belong to
    the old database; written into the vector file of the new one they would overwrite
    another chunk's vector."""
    import os

    from aleph_zero import embedder
    from aleph_zero.store import db

    index, store, chunk_ids = ready(repo)
    path = index_path(repo.root)
    assert db.set_aside(path, db.inode(path)) is True
    fresh = Index(open_db(path))
    fresh.sync(build_view(repo.root))
    assert store.add(chunk_ids, unit(52, len(chunk_ids))) == 0
    assert not store.path.exists() or os.path.getsize(store.path) == 0

    class Client:
        def embed(self, texts, kind):
            return unit(53, len(texts))

    report = embedder.embed_pending(index, store, Client(), budget_seconds=30)   # must come back, not spin
    assert report.embedded == 0 and report.seconds < 5


def test_the_scan_gives_the_same_scores_however_the_rows_lie_and_however_many_threads_take_part(monkeypatch):
    import numpy as np

    from aleph_zero import vectors

    random = np.random.default_rng(7)
    stored = random.standard_normal((5000, vectors.WIDTH), dtype=np.float32).astype(np.float16)
    query = stored[11].astype(np.float32)
    def plain(rows):        # the scan as it was before: rows gathered a block at a time
        out = np.empty(len(rows), dtype=np.float32)
        for start in range(0, len(rows), 512):
            out[start:start + 512] = stored[rows[start:start + 512]].astype(np.float32) @ query
        return out

    together = np.arange(5000, dtype=np.int64)                         # a fresh index: the view's rows lie side by side
    apart = np.delete(together, np.arange(0, 5000, 7))                 # rows of other branches in between
    few = np.array([3, 4, 5, 4000], dtype=np.int64)
    monkeypatch.setattr(vectors, "_BLOCK", 512)
    for rows in (together, apart, few, together[:1]):
        for threaded_from in (10**9, 1):                               # on one thread, then on several
            monkeypatch.setattr(vectors, "_THREADED_FROM", threaded_from)
            found = vectors.scores_of(stored, rows, query)
            assert found.dtype == np.float32
            if threaded_from > 1:
                assert np.array_equal(found, plain(rows))                 # a slice or a gather: the same to the last bit
            else:       # split otherwise among threads, a few products round their last bit the other way
                assert np.allclose(found, plain(rows), rtol=1e-5, atol=0)
                assert list(np.argsort(-found, kind="stable")[:20]) == list(np.argsort(-plain(rows), kind="stable")[:20])


def _store_of(tmp_path, count):
    """A store with `count` vectors in rows 0 to count-1, without a repository."""
    conn = open_db(tmp_path / "aleph-zero" / "index.db")
    store = VectorStore(conn, tmp_path / "aleph-zero", KIND)
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO chunks(chunk_id, chunk_hash, kind, symbol, signature, breadcrumb, text) VALUES (?, ?, 'function', '', '', '', '')",
                     ((i, f"h{i}") for i in range(1, count + 1)))
    conn.execute("INSERT INTO temp.view_chunks(chunk_id) SELECT chunk_id FROM chunks")
    conn.execute("COMMIT")
    matrix = unit(40, count)
    assert store.add(list(range(1, count + 1)), matrix) == count
    index = Index(conn)
    index._view_epoch = 1
    return conn, store, index, matrix


def _free(conn, first, last):
    """Collect the chunks whose vectors lie in rows first..last, as garbage collection does."""
    conn.execute("BEGIN")
    conn.execute("DELETE FROM temp.view_chunks WHERE chunk_id IN (SELECT chunk_id FROM chunk_vectors WHERE row BETWEEN ? AND ?)", (first, last))
    conn.execute("DELETE FROM chunk_vectors WHERE row BETWEEN ? AND ?", (first, last))
    conn.execute("COMMIT")


def test_the_blocks_of_free_rows_are_given_back_and_no_row_in_use_is_touched(tmp_path):
    import pytest

    block = 4096 // vectors.ROW_BYTES                # 8 rows to a 4 KB block
    conn, store, index, matrix = _store_of(tmp_path, 64 * block)
    before = np.fromfile(store.path, dtype=np.float16).reshape(-1, vectors.WIDTH).copy()
    mapped = np.memmap(store.path, dtype=np.float16, mode="r", shape=before.shape)      # as another session has it
    first, last = block * 10 + 5, block * 50 + 2     # a run whose two ends share their blocks with rows in use
    _free(conn, first, last)
    found = vectorfile.give_back(conn, store.path.parent)
    if found.get("unsupported"):
        pytest.skip(f"this file system gives nothing back: {found['unsupported']}")
    after = np.fromfile(store.path, dtype=np.float16).reshape(-1, vectors.WIDTH)
    whole = range(block * 11, block * 50)            # the blocks that hold free rows only
    assert found["bytes"] == len(whole) * vectors.ROW_BYTES and found["kinds"] == 1
    assert not after[whole.start:whole.stop].any()                                       # given back: they read as nothing
    in_use = [row for row in range(len(before)) if row < first or row > last]
    assert np.array_equal(after[in_use], before[in_use])                                # every row in use, to the bit
    assert np.array_equal(after[first:whole.start], before[first:whole.start])          # free, but beside rows in use: left
    assert np.array_equal(np.asarray(mapped)[in_use], before[in_use])                    # also through a mapping made before
    assert not np.asarray(mapped)[whole.start:whole.stop].any()                         # and what was given back reads as nothing there too: no fault
    assert store.path.stat().st_size == before.nbytes                                    # no row moved, the length stays
    assert store.path.stat().st_blocks * 512 <= before.nbytes - found["bytes"] + 16 * 4096
    index._view_epoch = 2
    expected = before[in_use].astype(np.float32) @ matrix[3]                             # what the rows in use scored before
    best = store.search(index, matrix[3], limit=5)
    assert [chunk_id - 1 for chunk_id, _ in best] == [in_use[at] for at in np.argsort(-expected, kind="stable")[:5]]
    assert np.allclose([score for _, score in best], np.sort(expected)[::-1][:5], atol=1e-6)
    assert vectorfile.give_back(conn, store.path.parent)["bytes"] == 0                       # again: nothing new, nothing harmed
    assert np.array_equal(np.fromfile(store.path, dtype=np.float16).reshape(-1, vectors.WIDTH)[in_use], before[in_use])


def test_a_row_given_back_is_used_again_and_holds_what_is_written_into_it(tmp_path):
    block = 4096 // vectors.ROW_BYTES
    conn, store, index, matrix = _store_of(tmp_path, 32 * block)
    _free(conn, block * 4, block * 20 - 1)
    vectorfile.give_back(conn, store.path.parent)
    count = 32 * block
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO chunks(chunk_id, chunk_hash, kind, symbol, signature, breadcrumb, text) VALUES (?, ?, 'function', '', '', '', '')",
                     ((i, f"n{i}") for i in range(count + 1, count + 4)))
    conn.execute("INSERT INTO temp.view_chunks(chunk_id) VALUES (?), (?), (?)", (count + 1, count + 2, count + 3))
    conn.execute("COMMIT")
    fresh = unit(41, 3)
    assert store.add([count + 1, count + 2, count + 3], fresh) == 3
    assert sorted(row[0] for row in conn.execute("SELECT row FROM chunk_vectors WHERE chunk_id > ?", (count,))) == [block * 4, block * 4 + 1, block * 4 + 2]
    index._view_epoch = 3
    best = store.search(index, fresh[1], limit=1)
    assert best[0][0] == count + 2 and best[0][1] > 0.99
    assert store.path.stat().st_size == count * vectors.ROW_BYTES


def test_a_file_system_that_gives_nothing_back_is_said_and_harms_nothing(tmp_path, monkeypatch):
    block = 4096 // vectors.ROW_BYTES
    conn, store, index, matrix = _store_of(tmp_path, 16 * block)
    before = store.path.read_bytes()
    _free(conn, block * 2, block * 10 - 1)

    def refuses(descriptor, offset, length):
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(vectorfile, "_punch", refuses)
    found = vectorfile.give_back(conn, store.path.parent)
    assert "not supported" in found["unsupported"].lower() and found["bytes"] == 0 and store.path.read_bytes() == before
    assert vectorfile.give_back(open_db(tmp_path / "other" / "index.db"), tmp_path / "other") == {"kinds": 0, "bytes": 0}     # no vectors at all


def test_only_rows_whose_freeing_is_committed_are_given_back(tmp_path):
    """A hole cannot be taken back. Rows freed inside a transaction that is then rolled back are still in use."""
    block = 4096 // vectors.ROW_BYTES
    conn, store, index, matrix = _store_of(tmp_path, 16 * block)
    before = store.path.read_bytes()
    conn.execute("BEGIN")
    conn.execute("DELETE FROM chunk_vectors WHERE row BETWEEN ? AND ?", (block * 2, block * 10 - 1))
    conn.execute("ROLLBACK")
    assert vectorfile.give_back(conn, store.path.parent)["bytes"] == 0 and store.path.read_bytes() == before


def test_a_session_that_collects_garbage_gives_the_freed_blocks_back(repo, monkeypatch):
    """The engine collects when it has stored new content and a day has passed; what that frees is given back then."""
    from aleph_zero import engine as engine_module
    from aleph_zero.engine import Engine

    asked = []
    monkeypatch.setattr(engine_module, "collect_if_due", lambda conn: {"chunks_removed": 3, "finished": True})
    monkeypatch.setattr(vectorfile, "give_back", lambda conn, directory: asked.append(directory) or {"kinds": 1, "bytes": 0})
    engine = Engine(repo.root)
    engine.search("render_invoice")                  # the first search stores content: a collection is due to be asked for
    assert asked == [index_path(repo.root).parent]
    monkeypatch.setattr(engine_module, "collect_if_due", lambda conn: None)       # not due: nothing to give back
    repo.write("src/new.py", "def another():\n    return 1\n")
    engine.search("another")
    assert len(asked) == 1
    monkeypatch.setattr(engine_module, "collect_if_due", lambda conn: {"chunks_removed": 1, "finished": True})
    monkeypatch.setattr(vectorfile, "give_back", lambda conn, directory: (_ for _ in ()).throw(OSError("the disk is gone")))
    repo.write("src/newer.py", "def one_more():\n    return 2\n")
    assert engine.search("one_more").hits               # never an error for the query that happens to trigger it


def test_gc_says_what_it_gave_back(repo, capsys):
    import json

    from aleph_zero import cli

    assert cli.main(["--cwd", str(repo.root), "gc", "--days", "0"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["given_back"] == {"kinds": 0, "bytes": 0}
