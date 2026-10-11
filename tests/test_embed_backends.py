import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "spikes"))

import embed_backends  # noqa: E402


def test_configs_cover_both_runtimes_and_both_quantizations():
    backends = {name: spec[0] for name, spec in embed_backends.CONFIGS.items()}
    assert backends["gguf-q8"] == "gguf" and backends["gguf-q4"] == "gguf"
    assert backends["onnx-q8"] == "onnx" and backends["onnx-q4"] == "onnx"


def test_unknown_config_is_rejected_by_name():
    with pytest.raises(KeyError, match="no-such-config"):
        embed_backends.fetch("no-such-config")


def test_failed_download_names_the_file(monkeypatch):
    def boom(repo_id, filename, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(embed_backends, "hf_hub_download", boom)
    with pytest.raises(embed_backends.FetchError, match="embeddinggemma-2-Q8_0.gguf"):
        embed_backends.fetch("gguf-q8")


def test_text_only_feed_gets_empty_modality_inputs():
    specs = [
        ("input_ids", "tensor(int64)", ["batch_size", "sequence_length"]),
        ("image_features", "tensor(float)", ["num_image_tokens", 512]),
        ("audio_features", "tensor(float)", ["num_audio_tokens", 512]),
    ]
    extra = embed_backends.empty_modality_inputs(specs, provided={"input_ids"})
    assert set(extra) == {"image_features", "audio_features"}
    assert extra["image_features"].shape == (0, 512)
    assert str(extra["image_features"].dtype) == "float32"


def test_unknown_required_input_is_reported_not_guessed():
    specs = [("position_ids", "tensor(int64)", ["batch_size", "sequence_length"])]
    with pytest.raises(embed_backends.UnsupportedModel, match="position_ids"):
        embed_backends.empty_modality_inputs(specs, provided=set())


def test_llama_server_runs_on_cpu_with_mmap_and_one_slot(tmp_path):
    argv = embed_backends.server_command(tmp_path / "m.gguf", threads=4, n_ctx=512, gpu=False, port=18080)
    assert argv[0].endswith("llama-build/bin/llama-server")
    assert argv[argv.index("-ngl") + 1] == "0"
    assert "--embedding" in argv
    assert "--no-mmap" not in argv and "--mlock" not in argv
    assert argv[argv.index("--parallel") + 1] == "1"
    assert argv[argv.index("--port") + 1] == "18080"


def test_llama_server_offloads_every_layer_when_gpu_is_requested(tmp_path):
    argv = embed_backends.server_command(tmp_path / "m.gguf", threads=4, n_ctx=512, gpu=True, port=1)
    assert argv[0].endswith("llama-build-metal/bin/llama-server")
    assert argv[argv.index("-ngl") + 1] == "99"


def test_llama_server_batch_covers_the_whole_context(tmp_path):
    # An embedding input must fit one physical batch; the default (512) rejects
    # an 824-token chunk with HTTP 500.
    argv = embed_backends.server_command(tmp_path / "m.gguf", threads=4, n_ctx=1024, gpu=False, port=1)
    assert argv[argv.index("-b") + 1] == "1024"
    assert argv[argv.index("-ub") + 1] == "1024"


def test_llama_server_can_listen_on_a_private_unix_socket(tmp_path):
    sock = tmp_path / "embed.sock"
    argv = embed_backends.server_command(tmp_path / "m.gguf", threads=4, n_ctx=512, gpu=True, socket_path=sock)
    assert argv[argv.index("--host") + 1] == str(sock)
    assert "--port" not in argv


def test_gpu_and_cpu_configs_exist_for_the_worker():
    assert embed_backends.CONFIGS["gguf-q8-gpu"][0] == "gguf-gpu"
    assert embed_backends.CONFIGS["gguf-q4"][0] == "gguf"
