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


def test_garbage_collection_drops_the_row_table_entry_and_no_row_is_ever_reused(repo):
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
    assert min(rows) > highest                                     # another session may still have the old rows mapped


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
    conn.executemany("INSERT INTO chunks(chunk_id, chunk_hash, kind, symbol, signature, breadcrumb, text) VALUES (?, ?, 'block', '', '', '', '')",
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
