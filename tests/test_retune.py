"""Tuning the ranking's values: each setting holds for a while and is put back; the table and what goes forward are plain arithmetic."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import retune  # noqa: E402

from aleph_zero import lexical, search  # noqa: E402


def test_a_setting_of_the_ranking_holds_inside_its_block_and_what_ships_comes_back():
    before = (lexical.WEIGHTS, lexical.LONG_QUERY, lexical.KEEP_TERMS, search.PATH_CANDIDATES, search.SYMBOL_CANDIDATES, dict(search.GENERATOR_WEIGHT))
    with retune.ranking(weights=(1.0, 1.0, 1.0, 1.0), long_query=(12, 16), path_candidates=8, symbol_candidates=20, symbol_weight=2.0):
        assert lexical.WEIGHTS == (1.0, 1.0, 1.0, 1.0) and (lexical.LONG_QUERY, lexical.KEEP_TERMS) == (12, 16)
        assert search.PATH_CANDIDATES == 8 and search.SYMBOL_CANDIDATES == 20 and search.GENERATOR_WEIGHT == {"symbol": 2.0}
    assert (lexical.WEIGHTS, lexical.LONG_QUERY, lexical.KEEP_TERMS, search.PATH_CANDIDATES, search.SYMBOL_CANDIDATES, dict(search.GENERATOR_WEIGHT)) == before
    with retune.ranking():                                   # nothing named: what ships
        assert lexical.WEIGHTS == before[0]


def test_every_pair_for_long_queries_keeps_at_least_as_many_words_as_the_length_it_cuts_at():
    assert all(keep >= cut for cut, keep in retune.VALUES["long_query"])
    assert all(retune.SHIPPED[name] in values for name, values in retune.VALUES.items())    # what ships is in every list


def _scores(title, full):
    return {"title": {"recall@10": title}, "full": {"recall@10": full}}


def test_the_table_reads_each_value_against_what_ships_and_long_queries_on_whole_text_only():
    names = ["q1", "q2"]
    by_variant = {
        retune.label("weights", retune.SHIPPED["weights"]): {"q1": _scores(1.0, 0.0), "q2": _scores(0.0, 0.0)},
        retune.label("weights", (1.0, 1.0, 1.0, 1.0)): {"q1": _scores(1.0, 1.0), "q2": _scores(0.0, 0.0)},
        retune.label("long_query", retune.SHIPPED["long_query"]): {"q1": _scores(1.0, 0.0), "q2": _scores(0.0, 0.0)},
        retune.label("long_query", (12, 16)): {"q1": _scores(0.0, 1.0), "q2": _scores(0.0, 1.0)},
    }
    table = retune.table(by_variant, names)
    assert table[retune.label("weights", (1.0, 1.0, 1.0, 1.0))] == {"measure": 0.5, "gain": 0.25}       # mean of the two forms: 0.25 -> 0.5
    assert table[retune.label("long_query", (12, 16))] == {"measure": 1.0, "gain": 1.0}                  # whole text only: 0.0 -> 1.0
    assert table[retune.label("weights", retune.SHIPPED["weights"])]["gain"] == 0.0


def test_at_most_two_changes_go_forward_one_a_value_and_only_at_a_point_and_a_half():
    table = {retune.label("weights", (1.0, 1.0, 1.0, 1.0)): {"measure": 0.6, "gain": 0.030},
             retune.label("weights", (2.0, 1.0, 1.0, 1.0)): {"measure": 0.6, "gain": 0.020},      # the same value's second best: not taken too
             retune.label("path_candidates", 8): {"measure": 0.6, "gain": 0.016},
             retune.label("symbol_candidates", 20): {"measure": 0.6, "gain": 0.015},              # a third: left behind
             retune.label("symbol_weight", 2.0): {"measure": 0.6, "gain": 0.014}}                 # under the bar
    assert retune.forward(table) == {"weights": (1.0, 1.0, 1.0, 1.0), "path_candidates": 8}
    assert retune.forward({retune.label("symbol_weight", 2.0): {"measure": 0.6, "gain": 0.014}}) == {}
