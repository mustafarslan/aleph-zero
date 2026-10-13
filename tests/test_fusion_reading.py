"""Fusing by definition, tried from the benchmark's side (docs/DEFERRED.md, entry 1c): what a definition is, and the arithmetic."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import fusion  # noqa: E402

from aleph_zero import search as ranker  # noqa: E402
from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_path, open_db  # noqa: E402
from aleph_zero.view import build_view  # noqa: E402

LONG = "def settle_accounts(ledger):\n" + "".join(
    f"    total_{n} = sum(entry.amount * {n} for entry in ledger if entry.kind == {n})\n" for n in range(60)) + "    return total_1\n"


def _chunks(index):
    return index.conn.execute(
        "SELECT bc.chunk_id, c.symbol, bc.scope_start FROM blob_chunks bc JOIN chunks c ON c.chunk_id = bc.chunk_id "
        "JOIN temp.view_blobs v ON v.blob_oid = bc.blob_oid AND v.family = bc.family ORDER BY bc.blob_oid, bc.ord").fetchall()


def test_only_the_pieces_of_one_named_definition_share_a_key(repo):
    repo.write("src/ledger.py", "RATE = 3\nLIMIT = 9\n\n\ndef small(a):\n    return a\n\n\ndef other(b):\n    return b\n\n\n" + LONG)
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    rows = _chunks(index)
    keys, shared = fusion.definition_keys(index, [row[0] for row in rows])
    pieces = [row[0] for row in rows if row[1] == "settle_accounts"]
    assert len(pieces) >= 2 and len({keys[piece] for piece in pieces}) == 1          # one long function: one candidate
    rest = [row[0] for row in rows if row[1] != "settle_accounts"]
    assert len({keys[chunk] for chunk in rest}) == len(rest)                         # short functions, the module's top level: each its own
    assert keys[pieces[0]] not in {keys[chunk] for chunk in rest} and shared == 0


def test_a_chunk_that_two_files_share_is_its_own_candidate_and_is_counted(repo):
    repo.write("src/one.py", LONG)
    repo.write("src/two.py", LONG)                                   # the same content twice: the same chunks, in two blobs... or one
    repo.write("src/three.py", LONG + "\n\ndef extra():\n    return 1\n")           # the same pieces in another file's content
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    rows = _chunks(index)
    keys, shared = fusion.definition_keys(index, sorted({row[0] for row in rows}))
    twice = [chunk for chunk in {row[0] for row in rows} if sum(row[0] == chunk for row in rows) > 1 and any(r[0] == chunk and r[2] for r in rows)]
    assert twice and shared == len(twice) and all(keys[chunk] == ("chunk", chunk) for chunk in twice)


def test_the_votes_for_two_pieces_of_one_definition_are_added_and_the_best_placed_piece_carries_them():
    keys = {1: ("def", "A"), 2: ("def", "A"), 3: ("chunk", 3), 4: ("chunk", 4)}
    rankings = {"lexical": [3, 2, 4], "dense": [1, 4]}             # words like piece 2, vectors piece 1: no piece has two votes today
    as_it_is = {chunk: score for chunk, score, _ in ranker.rrf(rankings)}
    assert as_it_is[4] > as_it_is[1] and as_it_is[4] > as_it_is[2]                   # so chunk 4, second in both, beats the function
    merged = fusion.by_definition(rankings, keys, close_up=False)
    assert [chunk for chunk, _, _ in merged][0] == 1               # the definition now leads, carried by its best-placed piece
    assert merged[0][2] == ("lexical", "dense") and 2 not in [chunk for chunk, _, _ in merged]
    assert abs(merged[0][1] - (1 / (60 + 2) + 1 / (60 + 1))) < 1e-12                 # its two votes, at the ranks its pieces had


def test_closing_up_moves_what_stood_behind_a_second_piece_and_merging_alone_does_not():
    keys = {1: ("def", "A"), 2: ("def", "A"), 3: ("chunk", 3)}
    rankings = {"lexical": [1, 2, 3]}
    merged = dict((chunk, score) for chunk, score, _ in fusion.by_definition(rankings, keys, close_up=False))
    closed = dict((chunk, score) for chunk, score, _ in fusion.by_definition(rankings, keys, close_up=True))
    assert merged[3] == 1 / (60 + 3) and closed[3] == 1 / (60 + 2) and merged[1] == closed[1] == 1 / (60 + 1)


def test_on_a_tie_the_piece_the_name_generator_named_carries_the_definition():
    keys = {1: ("def", "A"), 2: ("def", "A")}
    merged = fusion.by_definition({"lexical": [2], "symbol": [1]}, keys, close_up=False)
    assert [chunk for chunk, _, _ in merged] == [1]                # both first in their list: the name's piece, as the single-name rule expects


def test_the_engine_s_fusion_is_replaced_for_a_search_and_put_back(repo):
    repo.write("src/ledger.py", LONG)
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    before = ranker.rrf
    with fusion.fused(index, close_up=False) as seen:
        hits = ranker.search(index, view, "settle accounts ledger total", k=10)
        assert ranker.rrf is not before
    assert ranker.rrf is before and seen["searches"] == 1
    assert [hit.path for hit in hits][0] == "src/ledger.py"
