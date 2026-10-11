"""Time from process start to the MCP initialize response, for two launch commands."""
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

from aleph_zero import memstat

ROOT = Path(__file__).resolve().parents[2]
STUB = Path(__file__).with_name("stub_server.py")
LIMIT_SECONDS = 10.0
RUNS = 5
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "attach-probe", "version": "0"},
    },
}


def attach_once(command: list[str], measure_memory: bool) -> dict:
    started = time.perf_counter()
    proc = subprocess.Popen(
        command, cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True,
    )
    guard = threading.Timer(LIMIT_SECONDS * 3, proc.kill)
    guard.start()
    try:
        proc.stdin.write(json.dumps(INITIALIZE) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError(f"server exited before answering (code {proc.poll()}): {' '.join(command)}")
            message = json.loads(line)
            if message.get("id") == 1:
                if "error" in message:
                    raise RuntimeError(f"initialize failed: {message['error']}")
                break
        result = {"seconds": time.perf_counter() - started}
        if measure_memory:
            result["private_mb"] = memstat.private_bytes(proc.pid) / 1e6
            result["rss_mb"] = memstat.rss_bytes(proc.pid) / 1e6
        return result
    finally:
        guard.cancel()
        proc.kill()
        proc.wait()


def main() -> int:
    commands = {
        "uv_run_no_sync": ["uv", "run", "--no-sync", "python", str(STUB)],
        "venv_python": [sys.executable, str(STUB)],
    }
    report: dict = {"limit_seconds": LIMIT_SECONDS, "attach_seconds": {}}
    slowest = 0.0
    for name, command in commands.items():
        samples = [attach_once(command, measure_memory=False)["seconds"] for _ in range(RUNS)]
        report["attach_seconds"][name] = {
            "first": round(samples[0], 3),
            "median": round(statistics.median(samples), 3),
            "max": round(max(samples), 3),
        }
        slowest = max(slowest, max(samples))
    memory = attach_once(commands["venv_python"], measure_memory=True)
    report["server_private_mb"] = round(memory["private_mb"], 1)
    report["server_rss_mb"] = round(memory["rss_mb"], 1)
    report["pass"] = slowest < LIMIT_SECONDS
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
