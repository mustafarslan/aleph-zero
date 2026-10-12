"""The memory layer: the catalogue in the index, recall, staleness, and the Aeon adapter."""
import multiprocessing
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from aleph_zero import memory
from aleph_zero.memory import Cite, Memory
from aleph_zero.store import index_path, open_db, state_dir
from aleph_zero.view import build_view

AEON_LIB = Path(__file__).resolve().parents[1] / "bench" / "data" / "aeon-build" / "lib"
needs_aeon = pytest.mark.skipif(not list(AEON_LIB.glob("core.cpython-*.so")),
                                reason="the Aeon extension built by the M0 probe is not on this machine")


def unit(seed: int) -> np.ndarray:
    vector = np.random.default_rng(seed).standard_normal(256).astype(np.float32)
    return vector / np.linalg.norm(vector)


class StandIn:
    """A store of vectors with the adapter's interface: enough to test everything around it."""

    def __init__(self):
        self.rows: dict[str, np.ndarray] = {}

    def append(self, memory_id, session, text, vector, when):
        self.rows[memory_id] = np.asarray(vector, dtype=np.float32)

    def nearest(self, vector, limit):
        scored = sorted(self.rows.items(), key=lambda item: -float(item[1] @ np.asarray(vector, dtype=np.float32)))
        return [memory_id for memory_id, _ in scored[:limit]]

    def ids(self):
        return set(self.rows)


def fresh(repo, store=None) -> Memory:
    return Memory(open_db(index_path(repo.root)), store if store is not None else StandIn())


def test_a_memory_is_kept_with_what_it_cites_and_comes_back_by_meaning(repo):
    mem = fresh(repo)
    view = build_view(repo.root)
    first = mem.remember("chat", "Q: where are tokens refreshed?\nA: in refresh_token.", session="s1", vector=unit(1),
                         cites=[Cite("src/auth/token_store.py", view.files["src/auth/token_store.py"], 4, 6)])
    mem.remember("note", "Invoices are rendered nightly by render_invoice.", session="s1", vector=unit(2))
    found = mem.recall("anything", vector=unit(1), k=2, view=view)
    assert [item.memory_id for item in found][0] == first
    assert found[0].kind == "chat" and found[0].cites[0].path == "src/auth/token_store.py"
    assert not found[0].stale and found[0].created_at > 0


def test_without_vectors_memories_are_recalled_by_their_words_and_newest_first(repo):
    mem = fresh(repo)
    mem.remember("chat", "the zeppelin protocol is handled in gateway.py", session="s1", now=1000)
    mem.remember("chat", "billing totals are computed nightly", session="s1", now=2000)
    mem.remember("chat", "zeppelin retries were added last week", session="s2", now=3000)
    assert [item.text.split()[0] for item in mem.recall("zeppelin protocol", k=3)][:2] == ["the", "zeppelin"]
    assert [item.created_at for item in mem.recall("", k=2)] == [3000, 2000]      # nothing to match: the newest


def test_a_memory_is_stale_once_the_file_it_cites_has_changed(repo):
    mem = fresh(repo)
    view = build_view(repo.root)
    cited = mem.remember("chat", "render_invoice sums the lines", session="s1", vector=unit(3),
                         cites=[Cite("src/billing/invoice.py", view.files["src/billing/invoice.py"], 1, 3)])
    assert not mem.recall("render_invoice", vector=unit(3), k=1, view=view)[0].stale
    repo.write("src/billing/invoice.py", "def render_invoice(order):\n    return 'changed'\n")
    after = mem.recall("render_invoice", vector=unit(3), k=1, view=build_view(repo.root))[0]
    assert after.memory_id == cited and after.stale and after.cites[0].stale
    (repo.root / "src/billing/invoice.py").unlink()
    assert mem.recall("render_invoice", vector=unit(3), k=1, view=build_view(repo.root))[0].stale   # gone is changed too


def test_a_forgotten_memory_is_never_returned_whatever_the_store_holds(repo):
    store = StandIn()
    mem = fresh(repo, store)
    gone = mem.remember("chat", "a secret the user regrets typing", session="s1", vector=unit(4))
    kept = mem.remember("chat", "something else entirely", session="s1", vector=unit(5))
    assert mem.forget(gone) is True and mem.forget(gone) is False
    assert gone in store.ids()                                         # the store still has the event
    assert [item.memory_id for item in mem.recall("secret", vector=unit(4), k=5)] == [kept]
    assert "regrets" not in " ".join(row["text"] for row in mem.list())
    row = mem.conn.execute("SELECT text, deleted FROM memories WHERE memory_id = ?", (gone,)).fetchall()
    assert row == [("", 1)]                                            # the text is gone from the catalogue
    assert mem.forget_all() == 1 and mem.recall("", k=5) == []


def test_candidates_the_catalogue_does_not_know_are_ignored(repo):
    """The store may hold events of a catalogue that was rebuilt, or of another repository."""
    store = StandIn()
    store.append("not-in-the-catalogue", "s9", "x", unit(6), 1)
    mem = fresh(repo, store)
    known = mem.remember("chat", "the real one", session="s1", vector=unit(7))
    assert [item.memory_id for item in mem.recall("x", vector=unit(6), k=5)] == [known]


def test_a_failing_store_never_costs_the_memory_or_the_recall(repo):
    class Broken(StandIn):
        def append(self, *args):
            raise OSError("the disk is full")

        def nearest(self, *args):
            raise OSError("the file is gone")

    mem = fresh(repo, Broken())
    kept = mem.remember("chat", "kept in the catalogue anyway", session="s1", vector=unit(8))
    assert mem.conn.execute("SELECT embedded FROM memories WHERE memory_id = ?", (kept,)).fetchall() == [(0,)]
    assert [item.memory_id for item in mem.recall("kept catalogue", vector=unit(8), k=3)] == [kept]


def test_an_index_of_the_previous_schema_gains_the_memory_tables_in_place(repo):
    conn = open_db(index_path(repo.root))
    conn.execute("INSERT INTO blobs(blob_oid, family, kind, size, chunker_version, last_seen_at) VALUES ('a', 'text', 'text', 1, 4, 0)")
    for statement in ("DROP TABLE memory_cites", "DROP TABLE memories"):
        conn.execute(statement)
    conn.pragma("user_version", 6)
    conn.close()
    conn = open_db(index_path(repo.root))
    assert conn.pragma("user_version") == 7
    assert conn.execute("SELECT count(*) FROM blobs").fetchall() == [(1,)]
    assert conn.execute("SELECT count(*) FROM memories").fetchall() == [(0,)]


# ---- the adapter, against the real Aeon extension ---------------------------------------------------

@pytest.fixture
def aeon(repo, monkeypatch):
    monkeypatch.setenv("ALEPH_AEON_LIB", str(AEON_LIB))
    store = memory.open_store(state_dir(repo.root / ".git"))
    assert isinstance(store, memory.AeonStore)
    return store


@needs_aeon
def test_memories_go_through_aeon_and_come_back_by_meaning(repo, aeon):
    mem = fresh(repo, aeon)
    ids = [mem.remember("chat", f"memory number {n} about module_{n}", session="a-session-id-longer-than-thirty-five-bytes-" + str(n),
                        vector=unit(100 + n)) for n in range(40)]
    found = mem.recall("x", vector=unit(117), k=3)
    assert found[0].memory_id == ids[17] and found[0].text.startswith("memory number 17")
    assert aeon.ids() == set(ids)
    again = Memory(open_db(index_path(repo.root)), memory.open_store(state_dir(repo.root / ".git")))   # another session
    assert again.recall("x", vector=unit(103), k=1)[0].memory_id == ids[3]


@needs_aeon
def test_a_small_group_is_not_crowded_out_of_recall(repo, aeon):
    """Aeon's search has no filter and fetches a fixed number of candidates: the adapter asks for many more."""
    mem = fresh(repo, aeon)
    shared = unit(9)
    crowd = [mem.remember("chat", f"crowd {n}", session="crowd", vector=shared) for n in range(60)]
    for memory_id in crowd:
        mem.forget(memory_id)
    wanted = {mem.remember("chat", f"target {n}", session="target", vector=shared) for n in range(5)}
    assert {item.memory_id for item in mem.recall("x", vector=shared, k=5)} == wanted


def _write_from_another_process(root: str, lib: str, name: str, count: int) -> None:
    import os

    os.environ["ALEPH_AEON_LIB"] = lib
    mem = Memory(open_db(index_path(Path(root))), memory.open_store(state_dir(Path(root) / ".git")))
    for n in range(count):
        mem.remember("chat", f"{name} wrote {n}", session=name, vector=unit(hash((name, n)) % 10_000))


@needs_aeon
def test_two_processes_writing_at_once_lose_nothing(repo, aeon):
    context = multiprocessing.get_context("spawn")
    writers = [context.Process(target=_write_from_another_process, args=(str(repo.root), str(AEON_LIB), name, 60))
               for name in ("alpha", "bravo")]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(120)
    assert [writer.exitcode for writer in writers] == [0, 0]
    mem = fresh(repo, aeon)
    assert mem.conn.execute("SELECT count(*), count(DISTINCT memory_id), sum(embedded) FROM memories").fetchall() == [(120, 120, 120)]
    assert len(aeon.ids()) == 120


@needs_aeon
def test_a_store_file_that_was_removed_leaves_recall_by_words(repo, aeon):
    mem = fresh(repo, aeon)
    mem.remember("chat", "the zeppelin protocol lives in gateway.py", session="s1", vector=unit(11))
    for leftover in aeon.path.parent.glob(aeon.path.name + "*"):
        if not leftover.name.endswith(".lock"):
            leftover.unlink()
    assert [item.text for item in mem.recall("zeppelin protocol", vector=unit(11), k=3)] == ["the zeppelin protocol lives in gateway.py"]


def test_without_the_extension_there_is_no_store_and_nothing_fails(repo, monkeypatch, tmp_path):
    monkeypatch.setenv("ALEPH_AEON_LIB", str(tmp_path / "nothing-here"))
    monkeypatch.setitem(sys.modules, "aeon_py", None)
    store = memory.open_store(state_dir(repo.root / ".git"))
    assert store is None
    mem = Memory(open_db(index_path(repo.root)), store)
    kept = mem.remember("note", "remembered without any vector store", session="s1", vector=unit(12))
    assert [item.memory_id for item in mem.recall("remembered vector store", vector=unit(12), k=2)] == [kept]
