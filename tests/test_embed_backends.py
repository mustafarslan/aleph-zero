import sys
from pathlib import Path

import pytest

# The benchmark's own packages (`uv sync --group bench`); without them this file is skipped.
pytest.importorskip("huggingface_hub", reason="needs the bench group: uv sync --group bench")
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


@pytest.mark.skipif(
    not embed_backends.LLAMA_SERVERS["cpu"].exists()
    or not (embed_backends.DATA / "unsloth__embeddinggemma-2-GGUF" / "embeddinggemma-2-UD-Q4_K_XL.gguf").exists(),
    reason="needs the llama-server build and the Q4 model under bench/data (bench/spikes/build_llama.sh)",
)
def test_input_longer_than_the_token_cap_is_truncated_not_rejected():
    embedder = embed_backends.load("gguf-q4", n_ctx=64)
    try:
        long_text = embed_backends.DOC_PREFIX + " ".join(f"token_{i}" for i in range(400))
        assert embedder.token_count(long_text) > 64
        rows = embedder.embed([long_text, embed_backends.QUERY_PREFIX + "short"])
        assert rows.shape == (2, embed_backends.EXPECTED_DIM)
    finally:
        embedder.close()


def test_gpu_worker_uses_one_cpu_thread_and_cpu_worker_eight():
    # Measured: on Metal 1 thread and 8 threads both give ~59 chunks/s, but 8 threads
    # burn 183% of a core against 17%. On CPU, 8 threads is the optimum.
    assert embed_backends.worker_threads(gpu=True) == 1
    assert embed_backends.worker_threads(gpu=False) == 8


@pytest.mark.skipif(
    not embed_backends.LLAMA_SERVERS["cpu"].exists()
    or not (embed_backends.DATA / "unsloth__embeddinggemma-2-GGUF" / "embeddinggemma-2-UD-Q4_K_XL.gguf").exists(),
    reason="needs the llama-server build and the Q4 model under bench/data (bench/spikes/build_llama.sh)",
)
def test_many_requests_reuse_one_connection():
    # A connection per request exhausted the ephemeral port range after ~16k calls.
    embedder = embed_backends.load("gguf-q4", n_ctx=64)
    try:
        for i in range(300):
            assert embedder.token_count(f"value_{i}") > 0
        assert embedder.connections_opened == 1
    finally:
        embedder.close()


@pytest.mark.skipif(
    not embed_backends.LLAMA_SERVERS["cpu"].exists()
    or not (embed_backends.DATA / "unsloth__embeddinggemma-2-GGUF" / "embeddinggemma-2-UD-Q4_K_XL.gguf").exists(),
    reason="needs the llama-server build and the Q4 model under bench/data (bench/spikes/build_llama.sh)",
)
def test_requests_do_not_inherit_the_two_second_health_check_timeout():
    # A CPU chunk near capacity takes over 2 s; a 2 s socket timeout would make the
    # client give up and retry while the single-slot server is still computing.
    embedder = embed_backends.load("gguf-q4", n_ctx=64)
    try:
        embedder.token_count("x")
        assert embedder._conn.timeout >= 120
    finally:
        embedder.close()


def _fake_server(tmp_path, monkeypatch, script: str):
    """Stand in for llama-server with a shell script, and record the child it becomes."""
    binary = tmp_path / "llama-server"
    binary.write_text("#!/bin/sh\n" + script)
    binary.chmod(0o755)
    monkeypatch.setitem(embed_backends.LLAMA_SERVERS, "cpu", binary)
    started = []
    real_popen = embed_backends.subprocess.Popen

    def popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(embed_backends.subprocess, "Popen", popen)
    return started


def test_worker_child_is_stopped_when_start_up_fails_unexpectedly(tmp_path, monkeypatch):
    started = _fake_server(tmp_path, monkeypatch, "exec sleep 60\n")

    def broken_request(self, *args, **kwargs):
        raise ValueError("not a connection error")

    monkeypatch.setattr(embed_backends.GgufEmbedder, "_request", broken_request)
    with pytest.raises(ValueError):
        embed_backends.GgufEmbedder([tmp_path / "model.gguf"], threads=1, n_ctx=64)
    assert started[0].poll() is not None, "the llama-server child was left running"


def test_worker_that_dies_while_loading_reports_what_it_printed(tmp_path, monkeypatch):
    _fake_server(tmp_path, monkeypatch, "echo 'error: unknown model architecture' >&2\nexit 3\n")
    with pytest.raises(embed_backends.UnsupportedModel) as caught:
        embed_backends.GgufEmbedder([tmp_path / "model.gguf"], threads=1, n_ctx=64)
    assert "code 3" in str(caught.value)
    assert "unknown model architecture" in str(caught.value)


_ONNX_Q4 = embed_backends.DATA / "onnx-community__embeddinggemma-2-ONNX" / "onnx" / "model_q4.onnx"


@pytest.mark.skipif(not _ONNX_Q4.exists(), reason="needs the ONNX q4 export under bench/data")
def test_onnx_embedder_offers_the_same_surface_as_the_worker():
    import os

    embedder = embed_backends.load("onnx-q4", threads=1, n_ctx=64)
    try:
        assert embedder.pid == os.getpid()  # the model lives in this process
        assert embedder.token_count("def read_blob(oid): ...") > 3
    finally:
        embedder.close()
    embedder.close()  # closing twice is harmless
