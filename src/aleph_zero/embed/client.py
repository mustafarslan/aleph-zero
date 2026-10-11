"""Talking to the embedding worker over its UNIX socket.

One connection is kept for the client's lifetime: opening one per request exhausted the
machine's ephemeral ports after about 16,000 calls in M0. Every failure, whatever its
cause, is raised as Unavailable, because the caller's answer to all of them is the
same: go on without vectors.
"""
from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path

import numpy as np

MODEL = "embeddinggemma-2-q8"
PREFIX_VERSION = 1
WIDTH = 256                      # Matryoshka truncation of the model's 768, renormalised (ADR-001)
VECTOR_KIND = f"{MODEL}/p{PREFIX_VERSION}/{WIDTH}"   # stored with every vector; others are ignored
QUERY_PREFIX = "task: code retrieval | query: "
DOCUMENT_PREFIX = "title: none | text: "
FULL_WIDTH = 768
TRUNCATION_MARGIN = 8            # room for the special tokens the server adds back


class Unavailable(Exception):
    """The worker did not give a usable answer: absent, starting, busy too long, or gone."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class EmbedClient:
    def __init__(self, socket_path: Path, capacity: int = 4096, timeout: float = 120.0) -> None:
        self.socket_path = Path(socket_path)
        self.capacity = capacity
        self.timeout = timeout
        self._conn: _UnixConnection | None = None

    def embed(self, texts: list[str], kind: str) -> np.ndarray:
        """One unit-length row of WIDTH floats per text. `kind` is 'query' or 'document'."""
        prefix = {"query": QUERY_PREFIX, "document": DOCUMENT_PREFIX}[kind]
        rows = []
        for text in texts:
            status, data = self._request("POST", "/v1/embeddings", {"input": prefix + text})
            if status == 500 and b"too large" in data:
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
