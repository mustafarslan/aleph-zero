"""The sweep of the split threshold changes the chunker's setting for a while and puts it back."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import split_sweep as sweep  # noqa: E402

from aleph_zero.chunking import code as chunker  # noqa: E402
from aleph_zero.lang import PROFILES  # noqa: E402

LONG = "def long_one(values):\n" + "".join(f"    total_{n} = sum(value * {n} for value in values if value % {n + 1})\n" for n in range(60)) + "    return total_1\n"


def test_a_setting_holds_inside_its_block_and_the_shipped_one_comes_back():
    profile = PROFILES["python"]
    shipped = chunker.SPLIT_TOKENS
    at_shipped = len(chunker.code_chunks(profile, LONG).chunks)
    with sweep.split_at(96):
        assert chunker.SPLIT_TOKENS == 96
        assert len(chunker.code_chunks(profile, LONG).chunks) > at_shipped       # smaller pieces, more of them
    assert chunker.SPLIT_TOKENS == shipped and len(chunker.code_chunks(profile, LONG).chunks) == at_shipped


def test_a_setting_is_dropped_only_when_it_is_lower_with_an_interval_clear_of_zero():
    same = [1.0, 0.0] * 50
    lower = [0.0] * 60 + [1.0, 0.0] * 20                # clearly lower than `same`
    noisy = [1.0, 0.0] * 49 + [0.0, 0.0]                # one question lower: within the noise
    assert sweep.guard(same, lower)["dropped"] is True
    assert sweep.guard(same, noisy)["dropped"] is False
    assert sweep.guard(same, same) == {"difference": 0.0, "low": 0.0, "high": 0.0, "dropped": False}


def test_the_summary_reads_the_fused_arm_against_the_shipped_setting_when_every_setting_has_it():
    def question(lexical, fused, chunks=10):
        score = lambda hit: {"recall@10": hit, "recall@1": hit, "mrr": hit, "files": 6}  # noqa: E731
        return {"chunks": chunks, "title": {**score(lexical), "hybrid": score(fused)}, "full": {**score(lexical), "hybrid": score(fused)}}

    names = [f"q{n}" for n in range(40)]
    by_setting = {sweep.SHIPPED: {"questions": {name: question(0.0, 0.0) for name in names}, "chunks": 400},
                  96: {"questions": {name: question(0.0, 1.0, chunks=20) for name in names}, "chunks": 800}}
    summary = sweep.summarise(by_setting)
    assert summary["settings"][96]["title"]["hybrid"]["recall@10"] == 1.0 and summary["settings"][96]["chunks_against_shipped"] == 2.0
    assert summary["settings"][96]["title"]["hybrid"]["against_shipped"] == {"difference": 1.0, "low": 1.0, "high": 1.0, "dropped": False}
    assert summary["settings"][96]["title"]["against_shipped"]["difference"] == 0.0            # words and names did not move
    assert "against_shipped" not in summary["settings"][sweep.SHIPPED]["title"]["hybrid"]
