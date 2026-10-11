"""Download EmbeddingGemma 2 exports and embed text with llama.cpp or ONNX Runtime."""
from __future__ import annotations

import http.client
import json
import socket
import subprocess
import time
from pathlib import Path

import numpy as np
from huggingface_hub import hf_hub_download

DATA = Path(__file__).resolve().parents[1] / "data" / "models"
_BUILDS = Path(__file__).resolve().parents[1] / "data"
LLAMA_SERVERS = {
    "cpu": _BUILDS / "llama-build" / "bin" / "llama-server",
    "metal": _BUILDS / "llama-build-metal" / "bin" / "llama-server",
}
QUERY_PREFIX = "task: code retrieval | query: "
DOC_PREFIX = "title: none | text: "
TOKENIZER = ("google/embeddinggemma-2", "tokenizer.json")
GGUF_REPO = "unsloth/embeddinggemma-2-GGUF"
ONNX_REPO = "onnx-community/embeddinggemma-2-ONNX"

CONFIGS: dict[str, tuple[str, str, list[str]]] = {
    "gguf-bf16": ("gguf", GGUF_REPO, ["embeddinggemma-2-BF16.gguf"]),
    "gguf-q8": ("gguf", GGUF_REPO, ["embeddinggemma-2-Q8_0.gguf"]),
    "gguf-q4": ("gguf", GGUF_REPO, ["embeddinggemma-2-UD-Q4_K_XL.gguf"]),
    "gguf-q8-gpu": ("gguf-gpu", GGUF_REPO, ["embeddinggemma-2-Q8_0.gguf"]),
    "gguf-q4-gpu": ("gguf-gpu", GGUF_REPO, ["embeddinggemma-2-UD-Q4_K_XL.gguf"]),
    "gguf-bf16-gpu": ("gguf-gpu", GGUF_REPO, ["embeddinggemma-2-BF16.gguf"]),
    "onnx-q8": ("onnx", ONNX_REPO, ["onnx/model_quantized.onnx", "onnx/model_quantized.onnx_data"]),
    "onnx-q4": ("onnx", ONNX_REPO, ["onnx/model_q4.onnx", "onnx/model_q4.onnx_data"]),
}


EXPECTED_DIM = 768
TRUNCATION_MARGIN = 8  # room for the special tokens the server adds back


class FetchError(RuntimeError):
    """A model file could not be downloaded."""


class UnsupportedModel(RuntimeError):
    """The runtime loaded the file but does not produce the model's sentence embedding."""


def _download(repo: str, filename: str) -> Path:
    try:
        return Path(hf_hub_download(repo, filename, local_dir=DATA / repo.replace("/", "__")))
    except Exception as exc:
        raise FetchError(f"could not fetch {filename} from {repo}: {exc}") from exc


def fetch(name: str) -> list[Path]:
    if name not in CONFIGS:
        raise KeyError(f"{name}: unknown config, choose from {sorted(CONFIGS)}")
    _, repo, files = CONFIGS[name]
    return [_download(repo, filename) for filename in files]


def empty_modality_inputs(specs: list[tuple[str, str, list]], provided: set[str]) -> dict[str, np.ndarray]:
    """Zero-length tensors for the image/video/audio inputs a text-only call leaves unused.

    The EmbeddingGemma 2 export declares `<modality>_features` as float tensors of
    shape [n_tokens, width]. Any other required input is reported, never guessed.
    """
    extra: dict[str, np.ndarray] = {}
    for name, kind, shape in specs:
        if name in provided:
            continue
        if name.endswith("_features") and kind == "tensor(float)" and len(shape) == 2 and isinstance(shape[1], int):
            extra[name] = np.zeros((0, shape[1]), dtype=np.float32)
        else:
            raise UnsupportedModel(f"the ONNX graph needs an input this feed does not build: {name} {kind} {shape}")
    return extra


def _normalize(rows: np.ndarray) -> np.ndarray:
    rows = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(rows, axis=1, keepdims=True)
    return rows / np.maximum(norms, 1e-12)


def server_command(
    model: Path, threads: int, n_ctx: int, gpu: bool, port: int | None = None, socket_path: Path | None = None
) -> list[str]:
    """llama-server for embeddings: one slot, a batch as large as the context, weights mmapped.

    `gpu=True` uses the Metal build and offloads every layer. Listens on a private
    UNIX socket when `socket_path` is given, otherwise on 127.0.0.1:`port`.
    """
    listen = ["--host", str(socket_path)] if socket_path else ["--host", "127.0.0.1", "--port", str(port)]
    return [
        str(LLAMA_SERVERS["metal" if gpu else "cpu"]), "-m", str(model), "--embedding",
        "-ngl", "99" if gpu else "0", *listen,
        "-c", str(n_ctx), "-b", str(n_ctx), "-ub", str(n_ctx),
        "-t", str(threads), "--parallel", "1",
    ]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class GgufEmbedder:
    """Holds the model in a llama-server child process (the embed worker).

    llama-cpp-python 0.3.36 predates EmbeddingGemma 2 and its bindings crash against
    a newer libllama, so there is no in-process path. The server is built from a
    pinned llama.cpp commit with bench/spikes/llama-no-logits.patch applied.
    `pid` is the process whose memory holds the model.
    """

    def __init__(self, files: list[Path], threads: int, n_ctx: int, gpu: bool = False, socket_path: Path | None = None) -> None:
        binary = LLAMA_SERVERS["metal" if gpu else "cpu"]
        if not binary.exists():
            raise UnsupportedModel(f"llama-server is not built at {binary}; run bench/spikes/build_llama.sh")
        self.files = files
        self._n_ctx = n_ctx
        self.socket_path = socket_path
        self._conn = None
        self.connections_opened = 0
        self._port = None if socket_path else _free_port()
        self._proc = subprocess.Popen(
            server_command(files[0], threads, n_ctx, gpu, port=self._port, socket_path=socket_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.pid = self._proc.pid
        deadline = time.time() + 120
        while True:
            if self._proc.poll() is not None:
                raise UnsupportedModel(f"llama-server exited with code {self._proc.returncode} while loading {files[0].name}")
            try:
                if self._request("GET", "/health", None, timeout=2)[0] == 200:
                    # Drop the probe connection: it carries the 2 s health-check timeout,
                    # and a chunk near capacity takes longer than that on CPU.
                    self._conn.close()
                    self._conn = None
                    self.connections_opened = 0  # attempts during start-up do not count
                    return
            except (OSError, http.client.HTTPException):
                pass
            if time.time() > deadline:
                self.close()
                raise UnsupportedModel("llama-server did not become ready within 120 s")
            time.sleep(0.05)

    def _connect(self, timeout: float):
        self.connections_opened += 1
        if self.socket_path:
            return _UnixConnection(str(self.socket_path), timeout)
        return http.client.HTTPConnection("127.0.0.1", self._port, timeout=timeout)

    def _request(self, method: str, path: str, body: dict | None, timeout: float = 120) -> tuple[int, bytes]:
        """One keep-alive connection for the worker's lifetime; reconnect once if it drops.

        A connection per request exhausts the ephemeral port range after ~16k calls.
        """
        payload = None if body is None else json.dumps(body)
        for attempt in (0, 1):
            if self._conn is None:
                self._conn = self._connect(timeout)
            try:
                self._conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
                response = self._conn.getresponse()
                return response.status, response.read()
            except (http.client.HTTPException, OSError):
                self._conn.close()
                self._conn = None
                if attempt == 1:
                    raise
        raise AssertionError("unreachable")

    def token_count(self, text: str) -> int:
        status, data = self._request("POST", "/tokenize", {"content": text})
        if status != 200:
            raise UnsupportedModel(f"tokenize failed with HTTP {status}")
        return len(json.loads(data)["tokens"])

    def _truncate(self, text: str) -> str:
        """Cut text to the token cap. The server rejects over-length input instead of truncating it."""
        status, data = self._request("POST", "/tokenize", {"content": text})
        if status != 200:
            raise UnsupportedModel(f"tokenize failed with HTTP {status}")
        tokens = json.loads(data)["tokens"][: self._n_ctx - TRUNCATION_MARGIN]
        status, data = self._request("POST", "/detokenize", {"tokens": tokens})
        if status != 200:
            raise UnsupportedModel(f"detokenize failed with HTTP {status}")
        return json.loads(data)["content"]

    def embed(self, texts: list[str]) -> np.ndarray:
        rows = []
        for text in texts:
            status, data = self._request("POST", "/v1/embeddings", {"input": text})
            if status == 500 and b"too large" in data:
                status, data = self._request("POST", "/v1/embeddings", {"input": self._truncate(text)})
            if status != 200:
                raise UnsupportedModel(f"embedding failed with HTTP {status}: {data[:200].decode(errors='replace')}")
            vector = np.asarray(json.loads(data)["data"][0]["embedding"], dtype=np.float32)
            if vector.ndim != 1 or vector.shape[0] != EXPECTED_DIM:
                raise UnsupportedModel(f"llama-server returned shape {vector.shape}, expected ({EXPECTED_DIM},)")
            rows.append(vector)
        return _normalize(np.stack(rows))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._proc.poll() is None:
            self._proc.terminate()
            self._proc.wait(timeout=20)


class OnnxEmbedder:
    def __init__(self, files: list[Path], threads: int, n_ctx: int) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.files = files
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        self._session = ort.InferenceSession(str(files[0]), options, providers=["CPUExecutionProvider"])
        self._inputs = {item.name for item in self._session.get_inputs()}
        self._outputs = [item.name for item in self._session.get_outputs()]
        if "sentence_embedding" not in self._outputs:
            # Mean-pooling last_hidden_state would skip the model's dense layers.
            raise UnsupportedModel(f"no sentence_embedding output, only {self._outputs}")
        specs = [(item.name, item.type, list(item.shape)) for item in self._session.get_inputs()]
        self._extra = empty_modality_inputs(specs, provided={"input_ids", "attention_mask"})
        self._tokenizer = Tokenizer.from_file(str(_download(*TOKENIZER)))
        self._tokenizer.enable_truncation(max_length=n_ctx)

    def embed(self, texts: list[str]) -> np.ndarray:
        rows = []
        for text in texts:
            ids = np.asarray([self._tokenizer.encode(text).ids], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": np.ones_like(ids), **self._extra}
            outputs = self._session.run(None, {k: v for k, v in feed.items() if k in self._inputs})
            vector = outputs[self._outputs.index("sentence_embedding")][0]
            if vector.shape[0] != EXPECTED_DIM:
                raise UnsupportedModel(f"sentence_embedding has dim {vector.shape[0]}, expected {EXPECTED_DIM}")
            rows.append(vector)
        return _normalize(np.stack(rows))


def worker_threads(gpu: bool) -> int:
    """CPU threads for the embed worker: the GPU needs one, the CPU path peaks at eight."""
    return 1 if gpu else 8


def load(name: str, threads: int | None = None, n_ctx: int = 4096, socket_path: Path | None = None):
    """n_ctx is the worker's token capacity: 4096 (ADR-001). Threads default to worker_threads()."""
    files = fetch(name)
    backend = CONFIGS[name][0]
    gpu = backend == "gguf-gpu"
    threads = worker_threads(gpu) if threads is None else threads
    if backend == "onnx":
        return OnnxEmbedder(files, threads, n_ctx)
    return GgufEmbedder(files, threads, n_ctx, gpu=gpu, socket_path=socket_path)
