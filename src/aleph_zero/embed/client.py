"""Talking to the embedding worker over its UNIX socket.

One connection is kept for the client's lifetime: opening one per request exhausted the
machine's ephemeral ports after about 16,000 calls in M0. Every failure, whatever its
cause, is raised as Unavailable, because the caller's answer to all of them is the
same: go on without vectors.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy is loaded when vectors are made: the supervisor never needs it
    import numpy as np

MODEL = "embeddinggemma-2-q8"
PREFIX_VERSION = 1
WIDTH = 256                      # Matryoshka truncation of the model's 768, renormalised (ADR-001)
VECTOR_KIND = f"{MODEL}/p{PREFIX_VERSION}/{WIDTH}"   # stored with every vector; others are ignored
QUERY_PREFIX = "task: code retrieval | query: "
DOCUMENT_PREFIX = "title: none | text: "
FULL_WIDTH = 768
TRUNCATION_MARGIN = 8            # room for the special tokens the server adds back
CONNECT_BACKOFF = (0.01, 0.02, 0.05, 0.1, 0.2)   # seconds between tries when a connection is refused


class Unavailable(Exception):
    """The worker did not give a usable answer: absent, starting, busy too long, or gone."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        # A server whose queue of waiting connections is full refuses the next one at once
        # (macOS does not make it wait). A pool opens several together, and two sessions
        # may do so at the same moment, so a refusal is retried briefly while the socket
        # file is there. No file means no worker: that fails immediately.
        for pause in (*CONNECT_BACKOFF, None):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            try:
                self.sock.connect(self._path)
                return
            except ConnectionRefusedError:
                self.sock.close()
                if pause is None or not os.path.exists(self._path):
                    raise
                time.sleep(pause)


class EmbedPool:
    """Several connections to the worker, used at once: it has that many slots, and
    embedding through all of them is what fills the GPU."""

    def __init__(self, socket_path: Path, capacity: int = 2048, connections: int = 8, timeout: float = 120.0) -> None:
        self._clients = [EmbedClient(socket_path, capacity, timeout) for _ in range(max(1, connections))]

    def embed(self, texts: list[str], kind: str) -> "np.ndarray":
        """Rows in the order of `texts`. Raises Unavailable if any connection fails."""
        import numpy as np
        from concurrent.futures import ThreadPoolExecutor

        if not texts:
            return np.zeros((0, WIDTH), dtype=np.float32)
        used = self._clients[: min(len(self._clients), len(texts))]
        shares = [texts[offset::len(used)] for offset in range(len(used))]
        with ThreadPoolExecutor(max_workers=len(used)) as threads:
            parts = list(threads.map(lambda pair: pair[0].embed(pair[1], kind), zip(used, shares)))
        rows = np.empty((len(texts), WIDTH), dtype=np.float32)
        for offset, part in enumerate(parts):
            rows[offset::len(used)] = part
        return rows

    def close(self) -> None:
        for client in self._clients:
            client.close()


class EmbedClient:
    def __init__(self, socket_path: Path, capacity: int = 4096, timeout: float = 120.0) -> None:
        self.socket_path = Path(socket_path)
        self.capacity = capacity
        self.timeout = timeout
        self._conn: _UnixConnection | None = None

    def embed(self, texts: list[str], kind: str) -> np.ndarray:
        """One unit-length row of WIDTH floats per text. `kind` is 'query' or 'document'."""
        import numpy as np

        prefix = {"query": QUERY_PREFIX, "document": DOCUMENT_PREFIX}[kind]
        rows = []
        for text in texts:
            status, data = self._request("POST", "/v1/embeddings", {"input": prefix + text})
            if status in (400, 500) and (b"too large" in data or b"exceed_context_size" in data):
                # The server refuses input over a slot's capacity instead of cutting it.
                status, data = self._request("POST", "/v1/embeddings", {"input": self._truncate(prefix + text)})
            if status != 200:
                raise Unavailable(f"embedding failed with HTTP {status}: {data[:200].decode(errors='replace')}")
            try:
                vector = np.asarray(json.loads(data)["data"][0]["embedding"], dtype=np.float32)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise Unavailable(f"the worker's answer could not be read: {exc}") from exc
            if vector.shape != (FULL_WIDTH,):
                raise Unavailable(f"the worker returned shape {vector.shape}, expected ({FULL_WIDTH},)")
            rows.append(vector[:WIDTH])
        matrix = np.stack(rows) if rows else np.zeros((0, WIDTH), dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.maximum(norms, 1e-12)

    def token_count(self, text: str) -> int:
        status, data = self._request("POST", "/tokenize", {"content": text})
        if status != 200:
            raise Unavailable(f"tokenize failed with HTTP {status}")
        return len(json.loads(data)["tokens"])

    def metrics(self) -> str:
        status, data = self._request("GET", "/metrics", None)
        if status != 200:
            raise Unavailable(f"metrics failed with HTTP {status}")
        return data.decode(errors="replace")

    def healthy(self) -> bool:
        try:
            return self._request("GET", "/health", None, timeout=2.0)[0] == 200
        except Unavailable:
            return False
        finally:
            self.close()  # the probe's short timeout must not be inherited by real requests

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _truncate(self, text: str) -> str:
        """Cut text to the worker's capacity. The server rejects longer input instead of cutting it."""
        status, data = self._request("POST", "/tokenize", {"content": text})
        if status != 200:
            raise Unavailable(f"tokenize failed with HTTP {status}")
        tokens = json.loads(data)["tokens"][: self.capacity - TRUNCATION_MARGIN]
        status, data = self._request("POST", "/detokenize", {"tokens": tokens})
        if status != 200:
            raise Unavailable(f"detokenize failed with HTTP {status}")
        return json.loads(data)["content"]

    def _request(self, method: str, path: str, body: dict | None, timeout: float | None = None) -> tuple[int, bytes]:
        payload = None if body is None else json.dumps(body)
        for attempt in (0, 1):
            try:
                if self._conn is None:
                    self._conn = _UnixConnection(str(self.socket_path), timeout or self.timeout)
                self._conn.request(method, path, body=payload, headers={"Content-Type": "application/json"})
                response = self._conn.getresponse()
                return response.status, response.read()
            except TimeoutError as exc:
                self.close()
                raise Unavailable(f"the worker did not answer within {timeout or self.timeout} s") from exc
            except (http.client.HTTPException, OSError) as exc:
                self.close()
                if attempt == 1:
                    raise Unavailable(f"the worker cannot be reached: {exc}") from exc
        raise AssertionError("unreachable")
