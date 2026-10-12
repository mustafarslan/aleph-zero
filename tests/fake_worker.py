"""A stand-in for llama-server: the same endpoints on a UNIX socket, no model.

Run as: python fake_worker.py SOCKET [--capacity N] [--fail-start] [--sleep-idle-seconds N] [...]
Unknown flags are accepted and ignored, so it can be started with the real command line.
Vectors are a deterministic function of the text, so tests can tell them apart.
"""
import hashlib
import json
import math
import os
import socket
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

ARGS = sys.argv[1:]


def option(name, default=None):
    return ARGS[ARGS.index(name) + 1] if name in ARGS else default


SOCKET = option("--host")
CAPACITY = int(option("-c", "4096"))
STATE = {"connections": 0, "embeddings": 0, "started": time.time()}


def vector(text, dim=768):
    seed = hashlib.sha256(text.encode()).digest()
    values = [(seed[i % 32] / 255.0) - 0.5 + (i % 7) * 0.01 for i in range(dim)]
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        STATE["connections"] += 1

    def address_string(self):
        return "unix"

    def log_message(self, *args):
        pass

    def reply(self, status, payload, raw=False):
        body = payload.encode() if raw else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if raw else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok"})
        elif self.path == "/metrics":
            self.reply(200, f"llamacpp:n_decode_total {STATE['embeddings']}\nfake:connections_total {STATE['connections']}\n", raw=True)
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
        if self.path == "/tokenize":
            self.reply(200, {"tokens": list(range(len(body["content"].split())))})
        elif self.path == "/detokenize":
            self.reply(200, {"content": " ".join(f"t{token}" for token in body["tokens"])})
        elif self.path == "/v1/embeddings":
            text = body["input"]
            if "DROP_CONNECTION" in text and not STATE.get("dropped"):
                STATE["dropped"] = True
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)  # the client sees the connection end at once
                return
            if "HANG" in text:
                time.sleep(30)
            if "DIE" in text:
                os._exit(3)
            if len(text.split()) > CAPACITY:
                if "OLDSTYLE" in text:   # a single-slot server words its refusal differently
                    self.reply(500, {"error": {"message": "input is too large to process"}})
                else:
                    self.reply(400, {"error": {"code": 400, "type": "exceed_context_size_error",
                                               "message": "input is larger than the max context size. skipping"}})
                return
            STATE["embeddings"] += 1
            self.reply(200, {"data": [{"embedding": vector(text)}]})
        else:
            self.reply(404, {"error": "not found"})


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 5   # as small as the real server's: a full queue refuses a connection at once


if __name__ == "__main__":
    if "--fail-start" in ARGS or os.environ.get("FAKE_WORKER_FAIL"):
        print("error: unknown model architecture", file=sys.stderr)
        sys.exit(3)
    time.sleep(float(os.environ.get("FAKE_WORKER_LOAD_SECONDS", "0")))
    if os.path.exists(SOCKET):
        os.unlink(SOCKET)
    server = Server(SOCKET, Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
