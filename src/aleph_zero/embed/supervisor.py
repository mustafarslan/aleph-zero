"""The process that owns the worker: starts it (or adopts one whose supervisor died),
leaves when it dies, and stops it when nobody has used it for IDLE_EXIT seconds.

Run as `python -m aleph_zero.embed.supervisor --socket PATH [--adopt PID]`. Started
detached by `worker.ensure`; never by hand.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

from .. import memstat
from . import worker
from .client import EmbedClient, Unavailable


def _requests_served(client: EmbedClient) -> int | None:
    """The worker's count of decoded batches. Reading it does not wake a sleeping worker
    (measured); the `/slots` endpoint would."""
    try:
        for line in client.metrics().splitlines():
            if line.startswith("llamacpp:n_decode_total"):
                return int(float(line.split()[1]))
    except (Unavailable, ValueError, IndexError):
        return None
    finally:
        client.close()
    return None


def _cap_log(limit: int) -> None:
    """The server writes a line for every request: 187 MB after one benchmark run. Past the
    limit the log starts over; it is opened for appending, so writers carry on at the new end."""
    log = worker.home() / "worker.log"
    try:
        if log.stat().st_size > limit:
            os.truncate(log, 0)
    except OSError:
        pass


def _private_megabytes(pid: int) -> float | None:
    try:
        return memstat.private_bytes(pid) / 1e6
    except Exception:  # noqa: BLE001 - the process is gone, or its memory cannot be read: not this check's business
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--adopt", type=int)
    args = parser.parse_args(argv)
    poll = worker._seconds("ALEPH_WORKER_POLL_SECONDS", 60)
    idle_exit = worker._seconds("ALEPH_WORKER_IDLE_EXIT_SECONDS", 1800)
    memory_limit = worker._seconds("ALEPH_WORKER_MEMORY_MB", worker.MEMORY_LIMIT_MB)
    log_limit = int(worker._seconds("ALEPH_WORKER_LOG_BYTES", 8_000_000))

    child = None
    if args.adopt is None:
        child = subprocess.Popen(worker.server_command(), stdin=subprocess.DEVNULL)  # its output joins our log
        pid = child.pid
    else:
        pid = args.adopt
    worker.write_state({"supervisor_pid": os.getpid(), "worker_pid": pid, "socket": args.socket, "started_at": time.time()})

    def alive() -> bool:
        if child is not None:
            return child.poll() is None
        return worker.is_ours(pid)

    def leave(*_signal, reason: str = "") -> None:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
        elif child is None:
            worker._kill(pid)
        if worker.state().get("supervisor_pid") == os.getpid():  # not if another supervisor has taken over
            try:
                os.unlink(args.socket)
            except OSError:
                pass
            if reason:  # remembered, so that the next request says why and does not restart at once
                worker.write_state({"failed_at": time.time(), "reason": reason})
            else:
                worker.state_file().unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, leave)
    signal.signal(signal.SIGINT, leave)
    client = EmbedClient(args.socket, timeout=5.0)
    last_count, last_change = None, time.monotonic()
    while True:
        time.sleep(poll)
        if not alive():
            leave()
        _cap_log(log_limit)
        used = _private_megabytes(pid)
        if used is not None and used > memory_limit:
            leave(reason=f"the worker used {used:.0f} MB, over its limit of {memory_limit:.0f} MB, and was stopped")
        count = _requests_served(client)
        if count is not None and count != last_count:
            last_count, last_change = count, time.monotonic()
        if time.monotonic() - last_change >= idle_exit:
            leave()


if __name__ == "__main__":
    sys.exit(main())
