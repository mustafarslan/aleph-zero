"""Spike E: memory, speed and lifecycle of the shared embed worker.

Architecture under test (ADR-005): sessions hold no model; one llama-server
process per machine holds it, on the GPU when there is one, and serves every
session's queries and all indexing. Budget:

    sessions x session_baseline + worker <= 1024 MB     (4 sessions)
    worker <= 600 MB while indexing at the 4096-token capacity
"""
import argparse
import json
import os
import signal
import statistics
import tempfile
import time
from pathlib import Path

import embed_backends
from aleph_zero import memstat

RESULTS = Path(__file__).resolve().parents[1] / "data" / "results"
SESSIONS = 4
SESSION_BASELINE_MB = 60.8  # Task 3: private memory of the bare MCP server
MACHINE_LIMIT_MB = 1024.0
WORKER_LIMIT_MB = 600.0
TOKEN_CAP = 4096


def code_chunk(lines: int, tag: str) -> str:
    body = "".join(f"    value_{i} = transform(request.field_{i}, default={i})  # {tag}\n" for i in range(lines))
    return embed_backends.DOC_PREFIX + "def handler(request):\n" + body


def private_mb(pid: int) -> float:
    return round(memstat.private_bytes(pid) / 1e6, 1)


def timed(embedder, texts: list[str]) -> list[float]:
    samples = []
    for text in texts:
        started = time.perf_counter()
        embedder.embed([text])
        samples.append((time.perf_counter() - started) * 1e3)
    return samples


def evaluate(name: str) -> dict:
    try:
        started = time.perf_counter()
        embedder = embed_backends.load(name, n_ctx=TOKEN_CAP)
    except (embed_backends.UnsupportedModel, embed_backends.FetchError) as exc:
        return {"status": "unsupported", "error": f"{type(exc).__name__}: {exc}"}
    try:
        report: dict = {"status": "ok", "load_seconds": round(time.perf_counter() - started, 2)}
        report["model_mb"] = round(sum(f.stat().st_size for f in embedder.files) / 1e6, 1)
        report["private_after_load_mb"] = private_mb(embedder.pid)
        embedder.embed([embed_backends.QUERY_PREFIX + "warm up"])
        queries = timed(embedder, [embed_backends.QUERY_PREFIX + f"where is the request handler defined {i}" for i in range(40)])
        report["query_ms"] = round(statistics.median(queries), 1)
        report["private_after_queries_mb"] = private_mb(embedder.pid)
        typical, large = code_chunk(11, "x"), code_chunk(165, "x")
        report["typical_chunk_tokens"] = embedder.token_count(typical)
        report["large_chunk_tokens"] = embedder.token_count(large)
        embedder.embed([code_chunk(11, "warm")])
        typical_ms = timed(embedder, [code_chunk(11, f"t{i}") for i in range(64)])
        report["typical_chunk_ms"] = round(statistics.median(typical_ms), 1)
        report["typical_chunks_per_second"] = round(1e3 / statistics.mean(typical_ms), 1)
        large_ms = timed(embedder, [code_chunk(165, f"l{i}") for i in range(32)])
        report["large_chunk_ms"] = round(statistics.median(large_ms), 1)
        report["private_after_indexing_mb"] = private_mb(embedder.pid)
        report["rss_after_indexing_mb"] = round(memstat.rss_bytes(embedder.pid) / 1e6, 1)
        worker = max(report["private_after_queries_mb"], report["private_after_indexing_mb"])
        report["machine_mb_at_4_sessions"] = round(SESSIONS * SESSION_BASELINE_MB + worker, 1)
        report["pass"] = worker <= WORKER_LIMIT_MB and report["machine_mb_at_4_sessions"] <= MACHINE_LIMIT_MB
        return report
    finally:
        embedder.close()


def lifecycle(name: str, requests: int = 500) -> dict:
    """A worker on a private UNIX socket: memory stays flat, SIGTERM leaves nothing behind."""
    with tempfile.TemporaryDirectory(prefix="aleph-embed-") as directory:
        sock = Path(directory) / "embed.sock"
        try:
            embedder = embed_backends.load(name, n_ctx=TOKEN_CAP, socket_path=sock)
        except (embed_backends.UnsupportedModel, embed_backends.FetchError) as exc:
            return {"status": "unsupported", "error": f"{type(exc).__name__}: {exc}"}
        pid = embedder.pid
        try:
            mode = oct(sock.stat().st_mode & 0o777)
            readings = []
            for i in range(requests):
                kind = i % 5
                text = code_chunk(165 if kind == 0 else 11, f"r{i}") if kind < 2 else embed_backends.QUERY_PREFIX + f"query {i}"
                embedder.embed([text])
                if i >= 100 and i % 50 == 0:
                    readings.append(private_mb(pid))
            os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 10
            while time.time() < deadline and embedder._proc.poll() is None:
                time.sleep(0.05)
            exited = embedder._proc.poll() is not None
            return {
                "status": "ok",
                "requests": requests,
                "socket_mode": mode,
                "private_mb_min": min(readings),
                "private_mb_max": max(readings),
                "memory_flat": max(readings) - min(readings) <= 20,
                "exited_on_sigterm": exited,
                "exit_code": embedder._proc.returncode,
                "socket_removed": not sock.exists(),
            }
        finally:
            embedder.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", nargs="+", default=["gguf-q8-gpu", "gguf-q4-gpu", "gguf-q8", "gguf-q4"])
    parser.add_argument("--lifecycle-config", default="gguf-q8-gpu")
    args = parser.parse_args()
    report = {"configs": {name: evaluate(name) for name in args.configs}}
    report["lifecycle"] = lifecycle(args.lifecycle_config)
    report["pass"] = any(r.get("pass") for r in report["configs"].values()) and bool(
        report["lifecycle"].get("memory_flat") and report["lifecycle"].get("exited_on_sigterm")
    )
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "mem_share.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
