"""MCP over stdio: the SDK against a hand-written JSON-RPC loop.

Usage: uv run python bench/spikes/transport.py

Starts each server as a child, performs initialize, tools/list and tool calls, and
reads start-up time, private memory and time per call. The two servers are written
to a temporary directory so that this spike is one file. Needs the `mcp` package
(dev group) for the SDK side.
"""
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aleph_zero import memstat

MINI = r'''"""A hand-written MCP stdio server: just enough JSON-RPC for initialize, tools/list, tools/call."""
import json
import sys

TOOLS = [{
    "name": "ping", "description": "Echo text back.",
    "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "default": "pong"}}},
    "_meta": {"anthropic/alwaysLoad": True},
}]


def handle(message):
    method, ident = message.get("method"), message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": ident, "result": {
            "protocolVersion": message["params"].get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "aleph-mini", "version": "0"},
            "instructions": "Mini server for the transport measurement."}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}}
    if method == "tools/call":
        if "--engine" in sys.argv:
            import aleph_zero.search  # noqa: F401  what a real call would have loaded
            import tree_sitter_language_pack as pack
            pack.get_language("python")
        text = message["params"].get("arguments", {}).get("text", "pong")
        return {"jsonrpc": "2.0", "id": ident, "result": {"content": [{"type": "text", "text": text}]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": ident, "result": {}}
    if ident is not None:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return None  # a notification


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    reply = handle(json.loads(line))
    if reply is not None:
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
'''

SDK = r'''import sys
from mcp.server.mcpserver import MCPServer

server = MCPServer("aleph-sdk", instructions="SDK server for the transport measurement.")


@server.tool(meta={"anthropic/alwaysLoad": True})
def ping(text: str = "pong") -> str:
    """Echo text back."""
    if "--engine" in sys.argv:
        import aleph_zero.search  # noqa: F401
        import tree_sitter_language_pack as pack
        pack.get_language("python")
    return text


if __name__ == "__main__":
    server.run("stdio")
'''


def run(script, engine):
    cmd = [sys.executable, script] + (["--engine"] if engine else [])
    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    def call(payload, expect=True):
        proc.stdin.write(json.dumps(payload) + "\n"); proc.stdin.flush()
        return json.loads(proc.stdout.readline()) if expect else None
    call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "0"}}})
    ready = time.perf_counter() - started
    call({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect=False)
    listed = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    idle = memstat.private_bytes(proc.pid) / 1e6
    t = time.perf_counter(); call({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ping", "arguments": {"text": "x"}}}); first_call = time.perf_counter() - t
    t = time.perf_counter()
    for i in range(200): call({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {"name": "ping", "arguments": {"text": "x"}}})
    per_call = (time.perf_counter() - t) / 200
    after = memstat.private_bytes(proc.pid) / 1e6
    proc.stdin.close(); proc.wait(timeout=10)
    return {"ready_ms": round(ready * 1000), "idle_mb": round(idle, 1), "first_call_ms": round(first_call * 1000, 1),
            "per_call_ms": round(per_call * 1000, 3), "after_mb": round(after, 1), "tools": len(listed["result"]["tools"])}

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as scratch:
        for label, source in (("hand-written", MINI), ("sdk", SDK)):
            script = Path(scratch) / f"{label}.py"
            script.write_text(source)
            for engine in (False, True):
                results = [run(str(script), engine) for _ in range(3)]
                best = min(results, key=lambda r: r["ready_ms"])
                print(label, "with engine" if engine else "bare", best)
