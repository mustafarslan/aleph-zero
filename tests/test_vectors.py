import multiprocessing
import time

import numpy as np

from aleph_zero import vectors
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
