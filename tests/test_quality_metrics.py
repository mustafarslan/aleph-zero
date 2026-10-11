import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "spikes"))

import quality  # noqa: E402


def test_truncate_renormalizes_rows():
    rows = np.array([[3.0, 4.0, 12.0, 0.0]], dtype=np.float32)
    out = quality.truncate(rows, 2)
    assert out.shape == (1, 2)
    assert out[0] == pytest.approx([0.6, 0.8])


def test_metrics_for_a_hit_at_rank_two():
    scores = np.array([[0.1, 0.9, 0.8, 0.0]], dtype=np.float32)
    result = quality.metrics(scores, relevant=[2], k=10)
    assert result["recall"] == 1.0
    assert result["mrr"] == 0.5
    assert result["ndcg"] == pytest.approx(1 / np.log2(3))


def test_metrics_for_a_miss_outside_k():
    scores = np.array([[0.9, 0.8, 0.7, 0.1]], dtype=np.float32)
    result = quality.metrics(scores, relevant=[3], k=3)
    assert result == {"recall": 0.0, "mrr": 0.0, "ndcg": 0.0}
