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
