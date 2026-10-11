import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from aleph_zero.embed import client as embed_client
from aleph_zero.embed.client import EmbedClient, Unavailable

FAKE = Path(__file__).parent / "fake_worker.py"


@pytest.fixture
def short_dir():
    # A UNIX socket path may be at most 104 bytes on macOS; pytest's tmp_path is longer.
    directory = Path(tempfile.mkdtemp(prefix="az-", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture
def worker(short_dir):
    sock = short_dir / "w.sock"
    proc = subprocess.Popen([sys.executable, str(FAKE), "--host", str(sock), "-c", "64"])
    deadline = time.time() + 10
    while time.time() < deadline and not (sock.exists() and EmbedClient(sock).healthy()):
        time.sleep(0.02)  # the socket file appears before the server accepts connections
    yield sock
    proc.terminate()
    proc.wait(timeout=10)


def metric(client, name):
    return int(next(line.split()[1] for line in client.metrics().splitlines() if line.startswith(name)))


def test_vectors_are_256_wide_unit_length_and_differ_by_text(worker):
    client = EmbedClient(worker, capacity=64)
    rows = client.embed(["def refresh_token(session): ...", "class TokenStore: ..."], kind="document")
    assert rows.shape == (2, 256) and rows.dtype == np.float32
    assert np.allclose(np.linalg.norm(rows, axis=1), 1.0, atol=1e-5)
    assert not np.allclose(rows[0], rows[1])
    query = client.embed(["def refresh_token(session): ..."], kind="query")
    assert not np.allclose(query[0], rows[0])  # a query and a document are embedded with different prefixes
    client.close()


def test_many_requests_use_one_connection(worker):
    client = EmbedClient(worker, capacity=64)
    opened = metric(client, "fake:connections_total")   # includes this client's own connection
    for i in range(200):
        client.embed([f"value {i}"], kind="document")
    assert metric(client, "fake:connections_total") == opened  # one per request once exhausted the machine's ports
    client.close()


def test_a_dropped_connection_is_reopened_once(worker):
    client = EmbedClient(worker, capacity=64)
    client.embed(["warm"], kind="document")
    rows = client.embed(["DROP_CONNECTION please"], kind="document")
    assert rows.shape == (1, 256)
    client.close()


def test_input_over_the_capacity_is_truncated_by_tokens_not_rejected(worker):
    client = EmbedClient(worker, capacity=64)
    long_text = " ".join(f"word{i}" for i in range(500))
    assert client.token_count(long_text) > 64
    assert client.embed([long_text], kind="document").shape == (1, 256)
    assert client.embed(["OLDSTYLE " + long_text], kind="document").shape == (1, 256)  # both ways the server refuses
    client.close()


def test_a_worker_that_is_absent_slow_or_dies_is_unavailable_not_an_exception_of_its_own(worker, tmp_path):
    with pytest.raises(Unavailable):
        EmbedClient(tmp_path / "nobody.sock", capacity=64).embed(["x"], kind="document")
    slow = EmbedClient(worker, capacity=64, timeout=0.3)
    started = time.monotonic()
    with pytest.raises(Unavailable):
        slow.embed(["HANG forever"], kind="document")
    assert time.monotonic() - started < 2.0
    dying = EmbedClient(worker, capacity=64)
    with pytest.raises(Unavailable):
        dying.embed(["DIE now"], kind="document")
    with pytest.raises(Unavailable):
        dying.embed(["after death"], kind="document")


def test_the_identity_of_a_vector_names_model_prefixes_and_width():
    assert embed_client.VECTOR_KIND == "embeddinggemma-2-q8/p1/256"
    assert embed_client.QUERY_PREFIX == "task: code retrieval | query: "
    assert embed_client.DOCUMENT_PREFIX == "title: none | text: "


def test_a_pool_embeds_through_several_connections_and_keeps_the_order(worker):
    from aleph_zero.embed.client import EmbedPool

    texts = [f"chunk number {i}" for i in range(50)]
    single = EmbedClient(worker, capacity=64)
    expected = single.embed(texts, kind="document")
    opened = metric(single, "fake:connections_total")
    pool = EmbedPool(worker, capacity=64, connections=4)
    rows = pool.embed(texts, kind="document")
    assert rows.shape == (50, 256) and np.allclose(rows, expected)
    assert metric(single, "fake:connections_total") == opened + 4   # the pool's four
    assert pool.embed([], kind="document").shape == (0, 256)
    with pytest.raises(Unavailable):
        pool.embed(["fine", "DIE now", "fine"], kind="document")
    pool.close()
