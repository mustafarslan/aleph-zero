import sys
from pathlib import Path

import pytest

# The benchmark's own packages (`uv sync --group bench`); without them this file is skipped.
pytest.importorskip("huggingface_hub", reason="needs the bench group: uv sync --group bench")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "spikes"))

import mem_share  # noqa: E402

LIFECYCLE_OK = {"memory_flat": True, "exited_on_sigterm": True}


def test_capacity_is_4096_on_gpu_and_2048_on_cpu():
    assert mem_share.capacity_for("gguf-q8-gpu") == 4096
    assert mem_share.capacity_for("gguf-q8") == 2048


def test_gate_fails_when_the_cpu_fallback_fails_even_if_the_gpu_passes():
    configs = {"gguf-q8-gpu": {"pass": True}, "gguf-q8": {"pass": False}}
    assert mem_share.gate_passes(configs, LIFECYCLE_OK) is False


def test_gate_fails_when_a_required_configuration_was_not_run():
    assert mem_share.gate_passes({"gguf-q8-gpu": {"pass": True}}, LIFECYCLE_OK) is False


def test_gate_passes_only_with_every_required_configuration_and_a_clean_lifecycle():
    configs = {"gguf-q8-gpu": {"pass": True}, "gguf-q8": {"pass": True}}
    assert mem_share.gate_passes(configs, LIFECYCLE_OK) is True
    assert mem_share.gate_passes(configs, {"memory_flat": False, "exited_on_sigterm": True}) is False


def test_a_sample_larger_than_the_capacity_is_an_error_not_a_silent_truncation():
    mem_share.require_fits(tokens=3700, capacity=4096)
    with pytest.raises(ValueError, match="4137"):
        mem_share.require_fits(tokens=4137, capacity=4096)
