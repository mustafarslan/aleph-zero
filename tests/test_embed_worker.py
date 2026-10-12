import json
import multiprocessing
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from aleph_zero.embed import worker
from aleph_zero.embed.client import EmbedClient, Unavailable

FAKE = Path(__file__).parent / "fake_worker.py"


@pytest.fixture
def home(monkeypatch):
    """A worker home with a stand-in binary and model, in a directory short enough for a socket."""
    cache = Path(tempfile.mkdtemp(prefix="az-", dir="/tmp"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("ALEPH_WORKER_POLL_SECONDS", "0.2")
    monkeypatch.setenv("ALEPH_WORKER_LOAD_SECONDS", "10")
    root = worker.home()
    (root / "bin").mkdir(parents=True)
    binary = root / "bin" / "llama-server"
    binary.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    binary.chmod(0o755)
    (root / "models").mkdir()
    (root / "models" / worker.MODEL_FILE).write_bytes(b"not a real model")
    yield root
    worker.stop()
    for pid in _pids(root):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    shutil.rmtree(cache, ignore_errors=True)


def _pids(root):
    out = subprocess.run(["pgrep", "-f", str(root)], capture_output=True, text=True).stdout.split()
    return [int(pid) for pid in out if int(pid) != os.getpid()]


def _alive(pid):
    # A process that has exited but was not yet reaped by its parent still has an id.
    status = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(status) and not status.startswith("Z")


def _wait(condition, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_the_worker_home_is_private_and_its_socket_path_is_short(home):
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert len(os.fsencode(worker.socket_path())) < 100


def test_the_real_command_line(home):
    command = worker.server_command(gpu=True)
    joined = " ".join(command)
    assert command[0].endswith("bin/llama-server") and "--embedding" in command
    # Eight slots of 2,048 tokens, but never more than 4,096 tokens in one decode: measured
    # 104 chunks/s against 72 with one slot, and 490 MB at worst against 954 MB uncapped.
    assert "-ngl 99" in joined and "-t 1" in joined and "-c 16384 -b 4096 -ub 4096" in joined
    assert "--parallel 8" in joined and "--metrics" in joined and "--sleep-idle-seconds 600" in joined
    assert f"--host {worker.socket_path()}" in joined
    assert (worker.slots(gpu=True), worker.capacity_tokens(gpu=True)) == (8, 2048)
    cpu = " ".join(worker.server_command(gpu=False))
    assert "-ngl 0" in cpu and "-t 8" in cpu and "-c 2048 -b 2048 -ub 2048" in cpu and "--parallel 1" in cpu
    assert (worker.slots(gpu=False), worker.capacity_tokens(gpu=False)) == (1, 2048)


def test_ensure_starts_one_supervisor_and_one_worker_and_is_fast_afterwards(home):
    sock = worker.ensure()
    state = worker.state()
    assert _alive(state["supervisor_pid"]) and _alive(state["worker_pid"]) and state["supervisor_pid"] != state["worker_pid"]
    assert EmbedClient(sock, capacity=64).embed(["hello"], kind="document").shape == (1, 256)
    started = time.monotonic()
    assert worker.ensure() == sock
    assert time.monotonic() - started < 0.2 and worker.state() == state


def _ensure_in_child(cache, queue):
    os.environ["XDG_CACHE_HOME"] = cache
    try:
        queue.put(str(worker.ensure()))
    except Exception as exc:  # noqa: BLE001
        queue.put(f"error: {exc}")


def test_two_sessions_asking_at_once_start_one_worker(home):
    os.environ["FAKE_WORKER_LOAD_SECONDS"] = "0.5"
    try:
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        children = [context.Process(target=_ensure_in_child, args=(os.environ["XDG_CACHE_HOME"], queue)) for _ in range(3)]
        for child in children:
            child.start()
        results = {queue.get(timeout=30) for _ in children}
        for child in children:
            child.join()
    finally:
        del os.environ["FAKE_WORKER_LOAD_SECONDS"]
    assert results == {str(worker.socket_path())}
    fakes = subprocess.run(["pgrep", "-f", f"fake_worker.py.*--host {worker.socket_path()}"], capture_output=True, text=True)
    assert len(fakes.stdout.split()) == 1


def test_a_stale_socket_and_a_dead_worker_are_replaced(home):
    worker.socket_path().write_text("left behind by a crash")
    worker.state_file().write_text(json.dumps({"supervisor_pid": 999_999, "worker_pid": 999_998, "socket": str(worker.socket_path())}))
    sock = worker.ensure()
    assert EmbedClient(sock, capacity=64).healthy() and _alive(worker.state()["worker_pid"])


def test_a_worker_that_cannot_start_is_reported_with_its_reason_and_not_retried_at_once(home, monkeypatch):
    monkeypatch.setenv("FAKE_WORKER_FAIL", "1")
    with pytest.raises(Unavailable, match="unknown model architecture"):
        worker.ensure()
    started = time.monotonic()
    with pytest.raises(Unavailable, match="recently"):
        worker.ensure()
    assert time.monotonic() - started < 0.2 and _pids(home) == []
    monkeypatch.delenv("FAKE_WORKER_FAIL")
    monkeypatch.setenv("ALEPH_WORKER_COOLDOWN_SECONDS", "0")
    assert EmbedClient(worker.ensure(), capacity=64).healthy()  # after the cool-down it is tried again


def test_an_idle_worker_is_stopped_and_one_in_use_is_not(home, monkeypatch):
    monkeypatch.setenv("ALEPH_WORKER_IDLE_EXIT_SECONDS", "1.5")
    sock = worker.ensure()
    state = worker.state()
    client = EmbedClient(sock, capacity=64)
    for _ in range(12):  # three seconds of use: longer than the idle limit
        client.embed(["still here"], kind="document")
        time.sleep(0.25)
    assert _alive(state["worker_pid"]) and _alive(state["supervisor_pid"])
    client.close()
    assert _wait(lambda: not _alive(state["worker_pid"]) and not _alive(state["supervisor_pid"]), seconds=6)
    assert not worker.socket_path().exists() and not worker.state_file().exists()


def test_the_supervisor_leaves_when_the_worker_dies_and_the_next_request_starts_a_new_one(home):
    worker.ensure()
    first = worker.state()
    os.kill(first["worker_pid"], signal.SIGKILL)
    assert _wait(lambda: not _alive(first["supervisor_pid"]))
    assert not worker.socket_path().exists()
    worker.ensure()
    assert worker.state()["worker_pid"] != first["worker_pid"]


def test_a_worker_whose_supervisor_was_killed_is_adopted_not_doubled(home):
    sock = worker.ensure()
    first = worker.state()
    os.kill(first["supervisor_pid"], signal.SIGKILL)
    assert _wait(lambda: not _alive(first["supervisor_pid"]))
    assert _alive(first["worker_pid"])                   # nothing tells the worker its parent is gone
    assert worker.ensure() == sock
    assert _wait(lambda: worker.state().get("supervisor_pid") not in (None, first["supervisor_pid"]))
    second = worker.state()
    assert second["worker_pid"] == first["worker_pid"] and _alive(second["supervisor_pid"])
    os.kill(first["worker_pid"], signal.SIGKILL)         # and the adopting supervisor still notices its death
    assert _wait(lambda: not _alive(second["supervisor_pid"]))


def test_a_recycled_process_id_is_not_mistaken_for_the_worker(home):
    worker.state_file().write_text(json.dumps({"supervisor_pid": os.getpid(), "worker_pid": os.getpid(),
                                               "socket": str(worker.socket_path())}))
    sock = worker.ensure()   # this test process is alive but is not a worker for that socket
    assert worker.state()["worker_pid"] != os.getpid() and EmbedClient(sock, capacity=64).healthy()


def test_stop_ends_both_processes(home):
    worker.ensure()
    state = worker.state()
    worker.stop()
    assert _wait(lambda: not _alive(state["worker_pid"]) and not _alive(state["supervisor_pid"]))
    assert not worker.socket_path().exists()
    worker.stop()  # nothing to stop is not an error


def test_available_never_starts_anything(home):
    assert worker.available() is None and _pids(home) == []
    sock = worker.ensure()
    assert worker.available() == sock


def test_without_a_binary_or_model_the_worker_is_unavailable_with_a_hint(home):
    (home / "bin" / "llama-server").unlink()
    with pytest.raises(Unavailable, match="aleph embed setup"):
        worker.ensure()


def test_a_worker_that_outgrows_its_memory_limit_is_stopped_and_the_reason_is_kept(home, monkeypatch):
    monkeypatch.setenv("ALEPH_WORKER_MEMORY_MB", "1")   # any real process is over this
    sock = worker.ensure()
    state = worker.state()
    assert _wait(lambda: not _alive(state["worker_pid"]) and not _alive(state["supervisor_pid"]), seconds=6)
    assert not sock.exists()
    assert "over its limit of 1 MB" in worker.state().get("reason", "")
    with pytest.raises(Unavailable, match="over its limit"):   # and it is not restarted in a loop
        worker.ensure()
    monkeypatch.setenv("ALEPH_WORKER_MEMORY_MB", "100000")
    monkeypatch.setenv("ALEPH_WORKER_COOLDOWN_SECONDS", "0")
    assert EmbedClient(worker.ensure(), capacity=64).healthy()


def test_the_worker_keeps_nothing_between_requests(home):
    joined = " ".join(worker.server_command(gpu=True))
    # With the server's default prompt cache an embeddings worker slowed to a tenth of its
    # speed and grew past 1 GB over 4.5 hours of requests.
    assert "--cache-ram 0" in joined and "--no-cache-prompt" in joined
