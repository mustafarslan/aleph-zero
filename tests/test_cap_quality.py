import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "spikes"))

import cap_quality  # noqa: E402

SOURCE = b'''
class Store:
    def save(self, item, *, force=False):
        """Persist one item to disk, replacing any older copy.

        Longer explanation that must not become the query.
        """
        path = self.path_for(item)
        if force or not path.exists():
            path.write_bytes(item.encode())
        return path

    def undocumented(self):
        return 1


def helper(x):
    "Too short."
    return x
'''


def test_extracts_docstring_as_query_and_strips_it_from_the_code():
    functions = {f.breadcrumb: f for f in cap_quality.extract_functions(SOURCE, "pkg/store.py")}
    save = functions["Store > save"]
    assert save.query == "Persist one item to disk, replacing any older copy."
    assert save.signature.startswith("def save(self, item, *, force=False):")
    assert "Persist one item" not in "".join(save.statements)
    assert len(save.statements) == 3
    assert functions["Store > undocumented"].query is None
    assert functions["helper"].query is None  # under six words: not a usable query


def test_split_keeps_a_fitting_function_whole_and_packs_statements_otherwise():
    assert cap_quality.split_sizes([100, 100, 100], overhead=20, cap=512) == [[0, 1, 2]]
    assert cap_quality.split_sizes([100, 100, 100], overhead=20, cap=230) == [[0, 1], [2]]
    # A single statement larger than the cap still gets its own piece.
    assert cap_quality.split_sizes([50, 900, 50], overhead=20, cap=256) == [[0], [1], [2]]


def test_function_rank_is_the_best_rank_of_any_of_its_pieces():
    # 3 functions; function 1 has two pieces (chunks 1 and 2).
    func_of_chunk = np.array([0, 1, 1, 2])
    scores = np.array([[0.5, 0.1, 0.9, 0.7]], dtype=np.float32)
    assert cap_quality.function_ranks(scores, func_of_chunk, targets=[1]).tolist() == [1]
    assert cap_quality.function_ranks(scores, func_of_chunk, targets=[0]).tolist() == [3]


def test_paired_bootstrap_is_zero_for_identical_ranks_and_positive_for_a_clear_win():
    same = np.array([1, 3, 20, 2, 50] * 40)
    mean, low, high = cap_quality.paired_bootstrap(same, same, metric="recall@10")
    assert (mean, low, high) == (0.0, 0.0, 0.0)
    better = np.array([1] * 200)
    worse = np.array([1] * 100 + [50] * 100)
    mean, low, high = cap_quality.paired_bootstrap(better, worse, metric="recall@10")
    assert mean == 0.5 and low > 0.4 and high < 0.6
    mean, low, high = cap_quality.paired_bootstrap(better, worse, metric="mrr@10")
    assert mean == 0.5 and low > 0.4
