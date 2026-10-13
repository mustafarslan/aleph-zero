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


def test_a_language_is_read_from_the_repositories_the_product_calls_that_language_whatever_file_they_are_in():
    # A verdict is applied by a repository's main language, so that is what its questions are chosen by, not by a set's label.
    def some(repo, count, main, gain):
        return [{"instance": f"{repo}-{n}", "repo": repo, "chunks": 1000, "main_language": main,
                 "scores": {form: {"lexical": {"recall@10": 0.0, "recall@1": 0.0}, "hybrid": {"recall@10": gain, "recall@1": 0.0}} for form in ("title", "full")}}
                for n in range(count)]

    labelled_typescript = [*some("a/ts", 10, "typescript", 0.0), *some("b/js", 10, "javascript", 1.0)]
    labelled_javascript = [*some("c/js", 10, "javascript", 1.0), *some("d/js", 10, "javascript", 1.0), *some("e/js", 10, "javascript", 1.0)]
    rows = verdict.of_main_language([*labelled_typescript, *labelled_javascript], "javascript")
    assert len(rows) == 40 and {row["repo"] for row in rows} == {"b/js", "c/js", "d/js", "e/js"}
    assert verdict.read(rows)["passes"] is True
    assert [row["repo"] for row in verdict.of_main_language(labelled_typescript, "typescript")] == ["a/ts"] * 10


def test_a_question_whose_repository_s_language_was_never_read_is_not_quietly_counted_or_dropped():
    import pytest

    rows = [{"instance": "a-1", "repo": "a/a", "main_language": "go"}, {"instance": "a-2", "repo": "a/a"}]
    with pytest.raises(SystemExit, match="a-2"):
        verdict.of_main_language(rows, "go")


def test_the_same_question_in_two_files_is_refused():
    import pytest

    row = {"instance": "a-1", "repo": "a/a", "main_language": "go"}
    with pytest.raises(SystemExit, match="a-1"):
        verdict.of_main_language([row, dict(row)], "go")
