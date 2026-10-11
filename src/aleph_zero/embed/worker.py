"""The shared embedding worker: where it lives, how it is started, and when it stops.

One worker per user holds the model (ADR-005). Whichever session first needs vectors
starts it, under a lock, through a small supervisor process that owns it. The worker
unloads its model after IDLE_SLEEP seconds without a request (measured: 278 MB awake,
106 MB asleep, half a second to wake); the supervisor stops it after IDLE_EXIT.

Nothing here is on a query's path. A query asks `available()`, which only looks.
"""
from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .client import EmbedClient, Unavailable

MODEL_FILE = "embeddinggemma-2-Q8_0.gguf"
IDLE_SLEEP = 120          # seconds without a request before the worker unloads its model
SOCKET_LIMIT = 100        # a UNIX socket path may be 104 bytes on macOS, 108 on Linux


def _seconds(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def home() -> Path:
    """The worker's directory: binary, model, state, lock, log. Only the user may enter it."""
    root = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "aleph-zero" / "worker"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)  # the server creates its socket 0755; the directory is what keeps it private
    return root


def socket_path() -> Path:
    path = home() / "w.sock"
    if len(os.fsencode(path)) < SOCKET_LIMIT:
        return path
    short = Path(f"/tmp/aleph-zero-{os.getuid()}")  # a home directory too deep for a socket path
    short.mkdir(mode=0o700, exist_ok=True)
    if short.stat().st_uid != os.getuid():
        raise Unavailable(f"{short} belongs to someone else; the worker socket cannot be placed safely")
    short.chmod(0o700)
    return short / "w.sock"


def state_file() -> Path:
    return home() / "state.json"


def binary() -> Path:
    return home() / "bin" / "llama-server"


def model() -> Path:
    return home() / "models" / MODEL_FILE


def state() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def write_state(values: dict) -> None:
    scratch = state_file().with_suffix(".tmp")
    scratch.write_text(json.dumps(values))
    os.replace(scratch, state_file())


def server_command(gpu: bool | None = None) -> list[str]:
    """llama-server for embeddings.

    On the GPU: eight slots of 2,048 tokens, with at most 4,096 tokens in one decode.
    Measured on Django's chunks: 104 chunks/s against 72 with one slot; with every slot
    fed inputs at capacity, 490 MB of private memory at worst. Without the cap on the
    decode the same test reached 954 MB, over the worker's 600 MB limit (ADR-005).
    On the CPU: one slot, eight threads, as measured in M0."""
    gpu = sys.platform == "darwin" if gpu is None else gpu
    count, capacity = slots(gpu), capacity_tokens(gpu)
    batch = str(min(count * capacity, 4096))
    return [
        str(binary()), "-m", str(model()), "--embedding", "-ngl", "99" if gpu else "0",
        "--host", str(socket_path()), "-c", str(count * capacity), "-b", batch, "-ub", batch,
        "-t", "1" if gpu else "8", "--parallel", str(count), "--metrics", "--sleep-idle-seconds", str(IDLE_SLEEP),
    ]


def slots(gpu: bool | None = None) -> int:
    """How many inputs the worker takes at once; a client gains from that many connections."""
    gpu = sys.platform == "darwin" if gpu is None else gpu
    return 8 if gpu else 1


def capacity_tokens(gpu: bool | None = None) -> int:
    """The longest input one slot accepts. Chunks are cut at 256 tokens (ADR-001), so this is a safety net."""
    return 2048


def is_ours(pid: int | None) -> bool:
    """True if `pid` is a live process started for our socket. A bare "is it alive" would
    take a recycled process id for the worker."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return False
    return str(socket_path()) in command


def available() -> Path | None:
    """The socket, if a worker is answering right now. Starts nothing and waits for nothing."""
    path = socket_path()
    return path if path.exists() and EmbedClient(path).healthy() else None


@contextmanager
def _lock():
    handle = open(home() / "lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def ensure() -> Path:
    """The socket of a running worker, starting one if there is none. Raises Unavailable
    if it cannot be started; a failed start is not repeated until a cool-down has passed."""
    path = socket_path()
    if path.exists() and EmbedClient(path).healthy() and is_ours(state().get("supervisor_pid")):
        return path
    with _lock():
        known = state()
        healthy = path.exists() and EmbedClient(path).healthy()
        if healthy and is_ours(known.get("supervisor_pid")):
            return path  # another session started it while we waited for the lock
        if healthy and is_ours(known.get("worker_pid")):
            _spawn_supervisor(["--adopt", str(known["worker_pid"])])  # its supervisor was killed
            return path
        if is_ours(known.get("worker_pid")):
            if _wait_healthy(path, lambda: is_ours(known["worker_pid"])):
                return path  # it was still loading
            _kill(known["worker_pid"])
        failed = known.get("failed_at", 0)
        if time.time() - failed < _seconds("ALEPH_WORKER_COOLDOWN_SECONDS", 60):
            raise Unavailable(f"the worker failed to start recently: {known.get('reason', 'unknown reason')}")
        if not binary().exists() or not model().exists():
            raise Unavailable("the embedding worker is not installed; run `aleph embed setup`")
        path.unlink(missing_ok=True)  # left behind by a crash: the server does not remove its socket
        state_file().unlink(missing_ok=True)
        supervisor = _spawn_supervisor([])
        if _wait_healthy(path, lambda: supervisor.poll() is None):
            return path
        reason = _log_tail() or "it did not become ready in time"
        supervisor.terminate()
        _kill(state().get("worker_pid"))
        path.unlink(missing_ok=True)
        write_state({"failed_at": time.time(), "reason": reason})
        raise Unavailable(f"the worker could not be started: {reason}")


def stop() -> None:
    """Stop the worker and its supervisor, if there are any."""
    known = state()
    for key in ("supervisor_pid", "worker_pid"):
        if is_ours(known.get(key)):
            _kill(known[key])
    socket_path().unlink(missing_ok=True)
    state_file().unlink(missing_ok=True)


def _spawn_supervisor(extra: list[str]) -> subprocess.Popen:
    log = open(home() / "worker.log", "ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "aleph_zero.embed.supervisor", "--socket", str(socket_path()), *extra],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,  # outlives this session
        )
    finally:
        log.close()


def _wait_healthy(path: Path, still_trying) -> bool:
    deadline = time.monotonic() + _seconds("ALEPH_WORKER_LOAD_SECONDS", 120)
    while time.monotonic() < deadline:
        if path.exists() and EmbedClient(path).healthy():
            return True
        if not still_trying():
            return False
        time.sleep(0.05)
    return False


def _kill(pid) -> None:
    if not isinstance(pid, int):
        return
    for sign in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sign)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)


def _log_tail(limit: int = 600) -> str:
    try:
        data = (home() / "worker.log").read_bytes()[-limit:]
    except OSError:
        return ""
    lines = [line for line in data.decode(errors="replace").splitlines() if line.strip()]
    return " | ".join(lines[-3:])
