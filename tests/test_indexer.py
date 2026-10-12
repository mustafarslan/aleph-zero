import multiprocessing
import os
import time

from aleph_zero.indexer import Index, collect_garbage
from aleph_zero.search import search
from aleph_zero.store import index_path, open_db
from aleph_zero.view import View, build_view


def _index(repo):
    return Index(open_db(index_path(repo.root)))


def _total(index, table):
    return index.conn.execute(f"SELECT count(*) FROM {table}").fetchall()[0][0]


def test_first_sync_indexes_every_file_and_the_second_does_nothing(repo):
    index = _index(repo)
    first = index.sync(build_view(repo.root))
    assert (first.indexed, first.skipped, first.pending) == (3, 0, 0)
    again = index.sync(build_view(repo.root))
    assert (again.indexed, again.pending) == (0, 0)
    assert index.counts()["blobs_in_view"] == 3


def test_editing_one_file_indexes_one_blob_without_committing(repo):
    index = _index(repo)
    index.sync(build_view(repo.root))
    repo.write("src/billing/invoice.py", "def render_invoice(order):\n    return 'new'\n")
    report = index.sync(build_view(repo.root))
    assert report.indexed == 1


def test_identical_content_at_two_paths_is_stored_once(repo):
    repo.write("src/billing/copy.py", (repo.root / "src/billing/invoice.py").read_text())
    index = _index(repo)
    index.sync(build_view(repo.root))
    assert _total(index, "blobs") == 3


def test_a_function_shared_by_two_different_files_is_stored_once(repo):
    shared = "def shared_helper(value):\n    return value * 2\n"
    repo.write("a.py", shared)
    repo.write("b.py", shared + "\n\ndef only_in_b():\n    return 1\n")
    index = _index(repo)
    index.sync(build_view(repo.root))
    rows = index.conn.execute(
        "SELECT chunk_id, count(DISTINCT blob_oid) FROM blob_chunks GROUP BY chunk_id HAVING count(DISTINCT blob_oid) > 1").fetchall()
    assert len(rows) == 1 and rows[0][1] == 2


def test_switching_branches_back_and_forth_reindexes_nothing(repo):
    index = _index(repo)
    index.sync(build_view(repo.root))
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/billing/invoice.py", "feature = True\n")
    repo.commit("feature work")
    assert index.sync(build_view(repo.root)).indexed == 1
    repo.git("checkout", "-q", "main")
    assert index.sync(build_view(repo.root)).indexed == 0
    repo.git("checkout", "-q", "feature")
    assert index.sync(build_view(repo.root)).indexed == 0


def test_an_exhausted_budget_reports_pending_and_a_later_sync_finishes(repo):
    index = _index(repo)
    assert index.sync(build_view(repo.root), budget_seconds=0.0).pending == 3
    assert index.counts()["chunks_in_view"] == 0
    done = index.sync(build_view(repo.root))
    assert (done.indexed, done.pending) == (3, 0)


def test_a_binary_file_is_recorded_once_and_not_retried(repo):
    (repo.root / "logo.bin").write_bytes(b"\x00\x01\x02\x03")
    index = _index(repo)
    assert index.sync(build_view(repo.root)).skipped == 1
    repo.write("touch.txt", "anything\n")  # changes the view, forcing a real sync
    assert index.sync(build_view(repo.root)).skipped == 0
    assert index.conn.execute("SELECT kind FROM blobs WHERE kind LIKE 'skip:%'").fetchall() == [("skip:binary",)]


def test_garbage_collection_drops_old_content_but_never_the_current_view(repo):
    index = _index(repo)
    index.sync(build_view(repo.root))
    repo.write("src/billing/invoice.py", "replaced = True\n")
    repo.commit("replace")
    index.sync(build_view(repo.root))
    assert _total(index, "blobs") == 4
    removed = collect_garbage(index.conn, max_age_days=0, now=2_000_000_000)
    assert removed == {"blobs_removed": 1, "chunks_removed": 1, "finished": True}
    assert _total(index, "blobs") == 3
    assert index.sync(build_view(repo.root)).indexed == 0


def _sync_in_child(root, queue):
    from pathlib import Path

    index = Index(open_db(index_path(Path(root))))
    report = index.sync(build_view(Path(root)), budget_seconds=30)
    queue.put((report.indexed + report.skipped, report.pending))


def test_two_processes_indexing_the_same_repository_agree(repo):
    for i in range(120):
        repo.write(f"pkg/module_{i}.py", f"def function_{i}():\n    return {i}\n")
    repo.commit("many files")
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_sync_in_child, args=(str(repo.root), queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    results = [queue.get(timeout=60) for _ in workers]
    for worker in workers:
        worker.join()
        assert worker.exitcode == 0
    assert all(pending == 0 for _, pending in results)
    assert sum(done for done, _ in results) >= 123  # together they covered every blob
    index = _index(repo)
    assert index.sync(build_view(repo.root)).indexed == 0
    assert _total(index, "blobs") == 123
    assert _total(index, "chunks") == _total(index, "chunk_fts")
    orphans = index.conn.execute(
        "SELECT count(*) FROM blob_chunks bc LEFT JOIN chunks c ON c.chunk_id = bc.chunk_id WHERE c.chunk_id IS NULL").fetchall()
    assert orphans == [(0,)]


def test_a_file_that_vanishes_between_status_and_read_stays_pending_and_is_never_recorded(repo):
    repo.write("ephemeral.py", "here_then_gone = 1\n")
    view = build_view(repo.root)          # the view still lists the file
    (repo.root / "ephemeral.py").unlink()  # ...and it disappears before indexing
    index = _index(repo)
    report = index.sync(view)
    assert (report.indexed, report.skipped, report.pending) == (3, 0, 1)
    assert _total(index, "blobs") == 3  # a failed read says nothing about the content


def test_content_that_could_not_be_read_once_is_indexed_when_it_reappears(repo):
    index = _index(repo)
    repo.write("notes/draft.py", "def pangolin_marker():\n    return 1\n")
    view = build_view(repo.root)
    os.remove(repo.root / "notes/draft.py")
    index.sync(view)
    repo.write("src/restored.py", "def pangolin_marker():\n    return 1\n")  # the same bytes, elsewhere
    fresh = build_view(repo.root)
    assert index.sync(fresh).indexed == 1
    assert [hit.path for hit in search(index, fresh, "pangolin_marker")] == ["src/restored.py"]


def test_a_dirty_file_edited_after_the_view_was_built_is_not_stored_under_the_old_id(repo):
    index = _index(repo)
    path = "src/billing/invoice.py"
    repo.write(path, "def first_version():\n    return 'aardvark'\n")
    view = build_view(repo.root)
    repo.write(path, "def second_version():\n    return 'buffalo'\n")
    assert index.sync(view).pending == 1
    repo.write(path, "def first_version():\n    return 'aardvark'\n")
    fresh = build_view(repo.root)
    index.sync(fresh)
    assert search(index, fresh, "buffalo") == []
    assert [hit.path for hit in search(index, fresh, "aardvark")] == [path]


def test_a_blob_missing_from_the_object_store_is_counted_apart_and_never_recorded(repo):
    # A partial clone has tree entries whose blobs were never fetched. Such a blob is not
    # "pending": nothing this session does will make it readable, so no answer waits for it.
    index = _index(repo)
    real = build_view(repo.root)
    ghost = View(real.root, real.common_dir, real.object_format, real.tree,
                 {**real.files, "ghost.py": "0" * 40}, real.dirty)
    first = index.sync(ghost)
    assert (first.indexed, first.skipped, first.pending, first.unreadable) == (3, 0, 0, 1)
    again = index.sync(ghost)
    assert (again.pending, again.unreadable) == (0, 1)
    assert index.conn.execute("SELECT count(*) FROM blobs WHERE kind LIKE 'skip:%'").fetchall() == [(0,)]


def test_index_recovers_after_another_session_collects_its_content(repo):
    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)
    other = open_db(index_path(repo.root))  # a session with no view of its own
    collect_garbage(other, max_age_days=0, now=time.time() + 10)
    assert _total(index, "blobs") == 0
    assert index.sync(view).indexed == 3
    assert search(index, view, "render_invoice")[0].path == "src/billing/invoice.py"


def test_an_unchanged_view_still_marks_its_content_as_in_use(repo):
    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)
    index.conn.execute("UPDATE blobs SET last_seen_at = 0")
    index.sync(view)
    assert index.conn.execute("SELECT max(last_seen_at) FROM blobs").fetchall()[0][0] == 0  # not on every query
    index._touched_at -= 600   # some minutes later
    index.sync(view)
    assert index.conn.execute("SELECT min(last_seen_at) FROM blobs").fetchall()[0][0] > 0


def test_a_failed_sync_does_not_leave_the_index_answering_for_the_wrong_view(repo, monkeypatch):
    index = _index(repo)
    main = build_view(repo.root)
    index.sync(main)
    repo.write("src/extra.py", "def numbat_marker():\n    return 1\n")
    edited = build_view(repo.root)

    def fail(self):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(Index, "_refresh_view_chunks", fail)
    try:
        index.sync(edited)
    except RuntimeError:
        pass
    monkeypatch.undo()
    os.remove(repo.root / "src/extra.py")
    index.sync(main)  # the same fingerprint as before the failure
    assert index.counts()["blobs_in_view"] == 3


def test_an_empty_repository_syncs_to_nothing(empty_repo):
    report = _index(empty_repo).sync(build_view(empty_repo.root))
    assert (report.indexed, report.skipped, report.pending) == (0, 0, 0)


def test_content_indexed_by_an_older_version_is_indexed_again(repo, monkeypatch):
    from aleph_zero import indexer
    from aleph_zero.chunking import model

    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)
    # A build with an older chunker wrote these: seen from here as a change by another session.
    open_db(index_path(repo.root)).execute("UPDATE blobs SET chunker_version = chunker_version - 1")
    assert index.sync(view).indexed == 3
    assert search(index, view, "render_invoice")[0].path == "src/billing/invoice.py"
    collect_garbage(index.conn, max_age_days=30)
    assert _total(index, "chunks") == _total(index, "chunk_fts") == index.counts()["chunks_in_view"]


def test_view_follows_other_sessions_even_when_the_number_of_unindexed_blobs_is_unchanged(repo):
    index = _index(repo)
    index.sync(build_view(repo.root))
    repo.write("src/okapi.py", "def first():\n    return 'quollword'\n")
    repo.write("src/tapir.py", "def tapir():\n    return 2\n")
    view = build_view(repo.root)
    assert index.sync(view, budget_seconds=0.0).pending == 2

    other = Index(open_db(index_path(repo.root)))  # another session indexes one of the two...
    only_okapi = View(view.root, view.common_dir, view.object_format, view.tree,
                      {"src/okapi.py": view.files["src/okapi.py"]}, frozenset({"src/okapi.py"}))
    assert other.sync(only_okapi).indexed == 1
    with_token = view.files["src/auth/token_store.py"]  # ...and a third collects something we had
    other.conn.execute("DELETE FROM blobs WHERE blob_oid = ?", (with_token,))

    assert index.sync(view, budget_seconds=0.0).pending == 2  # two again, but not the same two
    assert [hit.path for hit in search(index, view, "quollword")] == ["src/okapi.py"]  # found by content


def test_path_words_are_kept_while_the_set_of_paths_is_unchanged(repo):
    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)
    words = index.path_words(view)
    repo.write("src/billing/invoice.py", "def render_invoice(order):\n    return 'edited'\n")
    edited = build_view(repo.root)
    index.sync(edited)
    assert index.path_words(edited) is words          # an edit moved no file
    repo.write("src/billing/receipt.py", "def render_receipt():\n    return 1\n")
    added = build_view(repo.root)
    index.sync(added)
    assert "receipt" in index.path_words(added)[1]    # a new path is seen
    assert index.path_words(added) is not words


def test_a_blob_another_session_stored_first_is_not_counted_as_indexed_here(repo):
    first, second = _index(repo), _index(repo)
    view = build_view(repo.root)
    assert first.sync(view).indexed == 3
    report = second.sync(view)
    assert (report.indexed, report.pending) == (0, 0)
    stolen = _index(repo)
    original = Index._prepare

    def prepare_after_someone_else(self, view, reader, key):
        item = original(self, view, reader, key)
        Index._prepare = original
        _index(repo).sync(build_view(repo.root))  # another session finishes first
        return item

    repo.write("src/new_one.py", "def numbat():\n    return 1\n")
    fresh = build_view(repo.root)
    Index._prepare = prepare_after_someone_else
    try:
        report = stolen.sync(fresh)
    finally:
        Index._prepare = original
    assert (report.indexed, report.pending) == (0, 0)


def test_a_failed_filter_does_not_leave_a_transaction_open(repo):
    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)

    def explode(path):
        raise RuntimeError("bad filter")

    try:
        index.narrow(view, "any", explode, "boom")
    except RuntimeError:
        pass
    index.narrow(view, "docs", None, None)  # would fail with "cannot start a transaction within a transaction"
    assert search(index, view, "invoices", kind="docs")


def test_a_huge_committed_file_is_skipped_without_being_read_into_memory(repo):
    import tracemalloc

    (repo.root / "cache.db").write_bytes(b"SQLite format 3\x00" + b"\x01\x02\x03\x04" * 5_000_000)  # 20 MB
    repo.commit("a cache someone committed")
    index = _index(repo)
    view = build_view(repo.root)
    tracemalloc.start()
    try:
        report = index.sync(view)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert (report.indexed, report.skipped, report.pending) == (3, 1, 0)
    assert index.conn.execute("SELECT kind, size FROM blobs WHERE kind LIKE 'skip:%'").fetchall() == [
        ("skip:too-large", 20_000_016)]
    assert peak < 8_000_000


def test_content_collected_under_a_live_view_is_counted(repo):
    """A session can tell that another session's collection took content it was using:
    it should never happen with the default ages, and the stress run checks that it does not."""
    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)
    assert index.lost_to_collection == 0
    repo.write("src/new_module.py", "def brand_new():\n    return 1\n")
    index.sync(build_view(repo.root))
    assert index.lost_to_collection == 0  # new content is not lost content
    collect_garbage(open_db(index_path(repo.root)), max_age_days=0, now=time.time() + 10)
    index.sync(build_view(repo.root))
    assert index.lost_to_collection == 4


def test_how_often_content_is_marked_as_in_use_can_be_set(repo, monkeypatch):
    from aleph_zero import indexer

    monkeypatch.setenv("ALEPH_SEEN_REFRESH_SECONDS", "0")
    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)
    index.conn.execute("UPDATE blobs SET last_seen_at = last_seen_at - 5")
    before = index.conn.execute("SELECT max(last_seen_at) FROM blobs").fetchall()[0][0]
    index.sync(view)
    assert index.conn.execute("SELECT min(last_seen_at) FROM blobs").fetchall()[0][0] > before
    assert indexer.seen_refresh_seconds() == 0


def test_content_collected_and_indexed_again_by_another_session_is_still_found(repo):
    """Session A has synced. A collection takes its content; session B, on the same files,
    indexes them again under new chunk ids. A's list of chunks must not go on pointing at
    the ids that were deleted."""
    first = _index(repo)
    view = build_view(repo.root)
    first.sync(view)
    assert search(first, view, "expire silently")[0].path == "docs/guide.md"
    collect_garbage(open_db(index_path(repo.root)), max_age_days=0, now=time.time() + 10)
    second = _index(repo)
    # Something else was indexed in between, so the new chunks do not get the old ids back.
    second.conn.executemany("INSERT INTO chunks(chunk_hash, kind, symbol, signature, breadcrumb, text) "
                            "VALUES (?, 'block', '', '', '', 'filler')", ((f"filler{n}",) for n in range(50)))
    assert second.sync(view).indexed == 3
    first.sync(view)
    assert search(first, view, "expire silently")[0].path == "docs/guide.md"


def test_an_unchanged_index_is_not_checked_again_on_every_sync(repo):
    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)
    checks = index.full_checks
    index.sync(view)
    index.sync(view)
    assert index.full_checks == checks                      # nobody wrote: nothing to look at
    other = open_db(index_path(repo.root))
    other.execute("UPDATE blobs SET last_seen_at = last_seen_at + 1")   # any commit by another connection
    index.sync(view)
    assert index.full_checks == checks + 1


def test_reading_gives_one_state_for_several_statements(repo):
    index = _index(repo)
    view = build_view(repo.root)
    index.sync(view)
    other = open_db(index_path(repo.root))
    with index.reading():
        before = _total(index, "blobs")
        collect_garbage(other, max_age_days=0, now=time.time() + 10)
        assert _total(index, "blobs") == before         # the collection is not seen half-way through
        index.narrow(view, "code", lambda path: path.startswith("src/"), key="src")  # temp tables still work
    assert _total(index, "blobs") == 0
    assert not index.undisturbed()


# ---- collection that runs by itself (M3) ------------------------------------------------------------------

def _replace_and_age(repo, index, days=40):
    """Leave one blob that no view uses any more, last seen `days` ago."""
    repo.write("src/billing/invoice.py", "replaced = True\n")
    repo.commit("replace")
    index.sync(build_view(repo.root))
    index.conn.execute("UPDATE blobs SET last_seen_at = ? WHERE (blob_oid, family) NOT IN "
                       "(SELECT blob_oid, family FROM temp.view_blobs)", (int(time.time() - days * 86400),))


def test_collection_within_a_time_limit_stops_and_says_it_has_not_finished(repo):
    index = _index(repo)
    index.sync(build_view(repo.root))
    _replace_and_age(repo, index)
    report = collect_garbage(index.conn, max_age_days=30, budget_seconds=0.0)
    assert report["finished"] is False and _total(index, "blobs") == 4
    report = collect_garbage(index.conn, max_age_days=30, budget_seconds=5.0)
    assert report == {"blobs_removed": 1, "chunks_removed": 1, "finished": True}


def test_old_content_is_collected_by_itself_once_a_day_after_a_sync_that_stored_something(repo):
    from aleph_zero import indexer

    index = _index(repo)
    index.sync(build_view(repo.root))
    assert indexer.collect_if_due(index.conn) is None            # a new index was "collected" when it was made
    _replace_and_age(repo, index)
    assert _total(index, "blobs") == 4
    index.conn.execute("UPDATE meta SET value = value - 90000 WHERE key = 'collected_at'")   # a day and an hour ago
    report = indexer.collect_if_due(index.conn)
    assert report["blobs_removed"] == 1 and _total(index, "blobs") == 3
    assert indexer.collect_if_due(index.conn) is None            # not again today


def test_only_one_session_collects_when_it_is_due(repo):
    from aleph_zero import indexer

    first, second = _index(repo), _index(repo)
    first.sync(build_view(repo.root))
    assert indexer.collect_if_due(first.conn) is None   # the index is new
    _replace_and_age(repo, first)
    first.conn.execute("UPDATE meta SET value = value - 90000 WHERE key = 'collected_at'")
    reports = [indexer.collect_if_due(first.conn), indexer.collect_if_due(second.conn)]
    assert [report is not None for report in reports] == [True, False]


def test_content_last_seen_within_the_age_is_kept_whoever_collects(repo):
    """Another session's view is protected by how recently it was seen, not by being this
    session's view."""
    from aleph_zero import indexer

    index = _index(repo)
    index.sync(build_view(repo.root))
    assert indexer.collect_if_due(index.conn) is None
    _replace_and_age(repo, index, days=2)       # used two days ago by some other worktree
    index.conn.execute("UPDATE meta SET value = value - 90000 WHERE key = 'collected_at'")
    assert indexer.collect_if_due(index.conn)["blobs_removed"] == 0
    assert _total(index, "blobs") == 4


def test_the_first_question_of_a_process_reads_only_the_paths_that_can_match(repo):
    from aleph_zero.search import path_ranking

    repo.write("src/billing/receipt_printer.py", "def print_receipt(): ...\n")
    repo.write("docs/Receipt-Layout.md", "# Layout\n")
    def _synced(repo):
        index, view = _index(repo), build_view(repo.root)
        index.sync(view)
        return index, view

    index, view = _synced(repo)
    index.path_words(view)
    for terms in (["receipt", "printer"], ["billing", "invoice"], ["token"], ["nothing", "here"], ["receipt"]):
        fresh, fresh_view = _synced(repo)
        first = path_ranking(fresh, fresh_view, terms)
        assert fresh._path_words is None                       # not every path was read for one question
        assert first == path_ranking(fresh, fresh_view, terms)  # the second builds the maps and agrees
        assert fresh._path_words is not None
        assert first == path_ranking(index, view, terms)


def test_an_older_build_does_not_redo_what_a_newer_build_indexed(repo, monkeypatch, capsys):
    """Two installed versions with different chunkers on one index took turns re-indexing everything."""
    import pytest

    from aleph_zero import indexer
    from aleph_zero.store import StoreError

    older, view = _index(repo), build_view(repo.root)
    assert older.sync(view).indexed == 3
    monkeypatch.setattr(indexer, "CHUNKER_VERSION", indexer.CHUNKER_VERSION + 1)
    newer = _index(repo)
    assert newer.sync(view).indexed == 3                     # an upgrade reads everything again, once
    monkeypatch.undo()
    with pytest.raises(StoreError, match="newer version of Aleph"):
        older.sync(view)                                     # ... and the older build leaves that alone, and says why
    from aleph_zero import cli
    assert cli.main(["--cwd", str(repo.root), "search", "refresh token"]) == 2
    assert "newer version of Aleph" in capsys.readouterr().err   # one sentence, not a traceback
    monkeypatch.setattr(indexer, "CHUNKER_VERSION", indexer.CHUNKER_VERSION + 1)
    assert newer.sync(view).indexed == 0                     # nothing of the newer build's was touched
    assert [hit.path for hit in search(newer, view, "refresh token")][:1] == ["src/auth/token_store.py"]
