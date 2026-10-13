"""The benchmark's own arithmetic: what a ranker's answer is taken to name, and what the summary says of it."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import benchmark_c as bench  # noqa: E402

from aleph_zero.indexer import Index  # noqa: E402
from aleph_zero.store import index_path, open_db  # noqa: E402
from aleph_zero.view import build_view  # noqa: E402


def test_a_ranking_of_chunks_is_read_to_ten_files_or_to_the_ten_chunks_an_agent_would_be_given(repo):
    repo.write("src/auth/more.py", "".join(f"def helper_{n}(value):\n    return value + {n}\n\n\n" for n in range(4)))
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    by_path: dict[str, list[int]] = {}
    for chunk_id, oid, family in index.conn.execute(
            "SELECT bc.chunk_id, bc.blob_oid, bc.family FROM blob_chunks bc JOIN temp.view_blobs v "
            "ON v.blob_oid = bc.blob_oid AND v.family = bc.family ORDER BY bc.ord"):
        for path in index.paths_for(oid, family):
            by_path.setdefault(path, []).append(chunk_id)
    many, other = by_path["src/auth/more.py"], by_path["src/billing/invoice.py"]
    assert len(many) >= 3
    ranking = [*many, *other]
    assert bench.chunk_files(index, ranking) == ["src/auth/more.py", "src/billing/invoice.py"]     # as deep as it takes
    # The engine's arms are scored on ten results, and several of them are in one file: the same cut for a ranking of chunks.
    assert bench.chunk_files(index, ranking, chunks=len(many)) == ["src/auth/more.py"]


def _entry(lexical, hybrid, dense, gold=("a.py",)):
    found = {form: {mode: dense for mode in bench.MODES} | {"lexical": lexical, "hybrid": hybrid} for form in bench.FORMS}
    return {"instance": "i", "repo": "o/r", "gold": list(gold), "files": found, "bm25": bench.score(list(gold), []),
            "scores": {form: {mode: bench.score(list(gold), found[form][mode]) for mode in bench.MODES} for form in bench.FORMS}}


def test_the_summary_says_how_many_files_each_ranker_s_answer_named():
    summary = bench.summarise([_entry(["a.py"], ["b.py", "a.py"], ["c.py"] * 1), _entry(["x.py", "y.py", "z.py"], ["a.py"], [])], {})
    assert summary["instances"] == 2 and summary["repos"] == ["o/r"]
    assert summary["title"]["lexical"]["files"] == 2.0 and summary["title"]["hybrid"]["files"] == 1.5
    assert summary["title"]["lexical"]["recall@10"] == 0.5 and summary["title"]["hybrid"]["recall@10"] == 1.0
    assert summary["title"]["hybrid_minus_lexical"]["recall@10"]["mean"] == 0.5
    assert "dense10" in bench.MODES     # vectors alone at the depth the engine's arms are read at


def test_at_most_one_result_a_file_is_cut_where_the_engine_cuts_its_list(repo):
    from aleph_zero.search import search

    repo.write("src/auth/more.py", "".join(f"def rotate_helper_{n}(token):\n    return rotate(token, {n})\n\n\n" for n in range(6)))
    repo.write("src/auth/other.py", "def rotate(token, n):\n    return token\n")
    index = Index(open_db(index_path(repo.root)))
    view = build_view(repo.root)
    index.sync(view)
    deep = search(index, view, "rotate token helper", k=50)
    as_it_is = [hit.path for hit in search(index, view, "rotate token helper", k=3)]
    assert [hit.path for hit in bench.cut(deep, 3)] == as_it_is                     # the same cut the engine makes
    assert as_it_is.count("src/auth/more.py") >= 2                                  # several results in one file
    one_each = [hit.path for hit in bench.cut(deep, 3, a_file=1)]
    assert len(set(one_each)) == len(one_each) == 3 and one_each[0] == as_it_is[0]  # three files, and first place is what it was
