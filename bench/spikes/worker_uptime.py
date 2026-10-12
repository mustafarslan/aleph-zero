"""Does the worker's memory grow with the number of requests, and which options stop it?

Usage: uv run python bench/spikes/worker_uptime.py [SECONDS per variant, default 75]

Found in M2: after 4.5 hours of embedding, the worker ran at a tenth of its speed and
held 1,008 MB. With the server's defaults (a prompt cache of up to 8 GB, context
checkpoints, slot matching by prompt similarity) memory grows by about 0.8 KB per
request and never levels off; with them off it stays flat. Seven minutes each:

  as shipped then        278 -> 329, 340, 345, 349, 353 MB
  caching off            277 -> 300, 281, 281, 281, 281 MB

The variant "as shipped" below starts from today's command line with the four options
removed, so it reproduces the old behaviour.
"""
import subprocess, sys, tempfile, threading, time
from pathlib import Path
from aleph_zero import memstat
from aleph_zero.embed import worker
from aleph_zero.embed.client import EmbedClient
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 75
variants = {"as shipped": [],
            "--cache-ram 0 --no-cache-prompt --ctx-checkpoints 0 -sps 0": ["--cache-ram", "0", "--no-cache-prompt", "--ctx-checkpoints", "0", "-sps", "0"]}
for label, extra in variants.items():
    sock = Path(tempfile.mkdtemp(prefix="az-u-", dir="/tmp")) / "w.sock"
    command = [arg if arg != str(worker.socket_path()) else str(sock) for arg in worker.server_command()]
    command = [arg for arg in command if arg not in ("--sleep-idle-seconds", str(worker.IDLE_SLEEP))]
    cut = command.index("--cache-ram")  # the options under test are the last ones of the command line
    command = command[:cut] + extra
    proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    probe = EmbedClient(sock)
    while not (sock.exists() and probe.healthy()): time.sleep(0.1)
    stop = time.monotonic() + SECONDS; counts = [0] * 8
    def run(k):
        client = EmbedClient(sock, capacity=2048); i = 0
        while time.monotonic() < stop:
            client.embed([f"def function_{k}_{i}(value_{i}):\n    return compute_{i}(value_{i}) + offset_{k} * {i}\n" * 3], kind="document"); i += 1
        counts[k] = i; client.close()
    threads = [threading.Thread(target=run, args=(k,)) for k in range(8)]
    start_mem = memstat.private_bytes(proc.pid) / 1e6; t0 = time.monotonic()
    for th in threads: th.start()
    marks = []
    while any(th.is_alive() for th in threads):
        time.sleep(SECONDS / 5); marks.append(round(memstat.private_bytes(proc.pid) / 1e6))
    for th in threads: th.join()
    total = sum(counts)
    print(f"{label:60} {total:6} requests, {total / SECONDS:5.0f}/s, memory {start_mem:.0f} -> {marks} MB ({(marks[-1] - marks[0]) * 1000 / max(total, 1):.1f} KB per request after warm-up)", flush=True)
    proc.terminate(); proc.wait(timeout=20)
