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


def test_gate_fails_when_only_the_reference_is_within_the_limit():
    report = {"configs": {
        "gguf-bf16-gpu": {"768-f32": {"ndcg_vs_reference": 1.0}},
        "gguf-q8-gpu": {"256-f16": {"ndcg_vs_reference": 0.90}},
    }}
    assert quality.gate_passes(report, chosen=("gguf-q8-gpu", "256-f16")) is False


def test_gate_fails_when_the_chosen_configuration_was_not_measured():
    report = {"configs": {"gguf-bf16-gpu": {"768-f32": {"ndcg_vs_reference": 1.0}}}}
    assert quality.gate_passes(report, chosen=("gguf-q8-gpu", "256-f16")) is False


def test_gate_passes_when_the_chosen_configuration_is_within_three_percent():
    report = {"configs": {"gguf-q8-gpu": {"256-f16": {"ndcg_vs_reference": 0.989}}}}
    assert quality.gate_passes(report, chosen=("gguf-q8-gpu", "256-f16")) is True


def test_gate_result_is_a_plain_bool_even_for_numpy_scores():
    # Scores reach the gate as numpy floats; a numpy bool cannot be written to JSON.
    report = {"configs": {"gguf-q8-gpu": {"256-f16": {"ndcg_vs_reference": np.float64(0.989)}}}}
    result = quality.gate_passes(report, chosen=("gguf-q8-gpu", "256-f16"))
    assert result is True
