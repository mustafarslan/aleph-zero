"""A language's benchmark result read by the gate of ADR-002: the same arithmetic for every language."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import verdict  # noqa: E402


def _question(repo, chunks, words, vectors):
    score = lambda hit: {"recall@10": hit, "recall@1": hit, "mrr": hit}  # noqa: E731
    return {"instance": f"{repo}-{chunks}-{words}-{vectors}", "repo": repo, "chunks": chunks,
            "scores": {form: {"lexical": score(words), "hybrid": score(vectors)} for form in ("title", "full")}}


def test_a_clear_gain_over_the_bar_in_enough_repositories_passes_and_says_on_what():
    rows = [_question(f"o/r{n % 5}", 1000, 0.0, 1.0 if n % 3 else 0.0) for n in range(120)]
    read = verdict.read(rows)
    assert read["questions"] == 120 and read["repositories"] == 5
    first = read["first line"]
    assert first["words"] == 0.0 and round(first["gain"], 3) == 0.667 and first["low"] > 0.05 and first["better"] == 80 and first["worse"] == 0
    assert read["passes"] is True and "by repository" in first


def test_a_gain_whose_interval_holds_zero_or_that_is_under_the_bar_does_not_pass():
    noisy = [_question(f"o/r{n % 5}", 1000, 1.0 if n % 2 else 0.0, 0.0 if n % 2 else 1.0) for n in range(40)]
    assert verdict.read(noisy)["passes"] is False                      # as many worse as better
    small = [_question(f"o/r{n % 5}", 1000, 0.0, 1.0 if n < 4 else 0.0) for n in range(120)]       # +3.3 points
    read = verdict.read(small)
    assert read["passes"] is False and read["first line"]["low"] > 0   # real, and under the bar


def test_fewer_than_four_repositories_is_no_verdict_and_the_smaller_repositories_are_read_beside():
    rows = [_question(f"o/r{n % 3}", 1000, 0.0, 1.0) for n in range(60)]
    read = verdict.read(rows)
    assert read["passes"] is None and "four" in read["why"]
    mixed = ([_question(f"o/small{n % 4}", 20_000, 0.0, 1.0) for n in range(40)]
             + [_question("o/huge", 90_000, 0.0, 0.0) for _ in range(40)] + [_question("o/huge", 20_000, 0.0, 0.0)])
    read = verdict.read(mixed)
    beside = read["repositories at or under 50,000 chunks"]
    assert beside["questions"] == 40 and beside["left out"] == ["o/huge"] and beside["first line"]["gain"] == 1.0     # a repository is out as a whole
