"""The MCP server: JSON-RPC over stdio, written by hand (ADR-009).

One line of JSON per message. The main thread reads requests and answers the protocol's
own methods at once; tool calls run one at a time on a worker thread, so a cancellation
or a ping is seen while a search is still running. Nothing but protocol messages is
ever written to standard output.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from dataclasses import asdict
from typing import IO

from . import gitio, outline, render
from .engine import Cancelled, Engine
from .store import StoreError

# Versions of the protocol this server speaks, newest first. A client proposing one of
# them gets it; any other proposal gets the newest. Older versions allowed several requests
# in one message, which this server does not read, so they are not offered. The 2026
# handshake (`server/discover`) is answered with "method not found", on which clients fall
# back to `initialize`.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
SERVER_INFO = {"name": "aleph-zero", "version": "0.1.0"}

INSTRUCTIONS = """\
Aleph Zero is a local index of this repository's code and documentation. It always \
matches the working tree, including uncommitted edits, so its answers are never stale.

Use it first when you need to find something and do not already know where it is:

- search(query): find code or docs by what they do or by name. Plain language \
("where are refresh tokens rotated") and identifiers ("TokenStore.get") both work. \
Each result is `path:start-end  kind  name`, a signature and a snippet. Prefer this \
to grep-then-read when you are locating an implementation; use Grep when you need \
every literal occurrence of a string.
- symbol(name): where a function, class, constant or field is defined. Qualify to \
narrow: `TokenStore.get`, `auth::TokenStore`.
- outline(path): what a file defines, with line ranges, instead of reading the file.
- status(): what is indexed for this worktree.

Then Read only the line ranges the results point to.

Options: kind="code" or "docs"; path_glob="src/api/*"; lang="python"; \
detail="locations" for more results in fewer tokens, detail="full" for whole \
definitions. Text shown earlier in the session is replaced by a pointer; ask with \
detail="full" to see it again. A note that files are "not indexed yet" means the \
index is still being built: results are partial and a retry soon will be complete.
"""

_COMMON = {
    "format": {"type": "string", "enum": ["text", "json"], "default": "text"},
    "cwd": {"type": "string", "description": "A directory inside the worktree to ask about. Default: the session's."},
}
_TIMEOUT = {"timeout_ms": {"type": "integer", "minimum": 1, "maximum": 600_000,
                           "description": "Answer within this time; a partial answer says so."}}
TOOLS = [
    {
        "name": "search",
        "description": ("Find code and documentation in this repository by meaning or by name. Returns ranked "
                        "locations (path:lines, kind, name, signature) with snippets, always matching the working "
                        "tree. Use before grep and Read when locating an implementation."),
        "inputSchema": {"type": "object", "required": ["query"], "properties": {
            "query": {"type": "string", "description": "Plain language or identifiers."},
            "k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
            "kind": {"type": "string", "enum": ["any", "code", "docs"], "default": "any"},
            "path_glob": {"type": "string", "description": "Only paths matching this pattern, e.g. src/api/*"},
            "lang": {"type": "string", "description": "Only this language, e.g. python, ts, go, rust, java, cpp"},
            "detail": {"type": "string", "enum": list(render.DETAILS), "default": "snippets"},
            **_COMMON, **_TIMEOUT}},
        "_meta": {"anthropic/alwaysLoad": True},
    },
    {
        "name": "outline",
        "description": "List what a file defines (functions, classes, fields, headings) with line ranges, "
                       "instead of reading the whole file.",
        "inputSchema": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "A path relative to the top of the worktree."},
            **_COMMON, **_TIMEOUT}},
    },
    {
        "name": "symbol",
        "description": "Find where a name is defined: function, method, class, constant or field. "
                       "A qualified name narrows it (TokenStore.get).",
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"},
            "k": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            **_COMMON, **_TIMEOUT}},
    },
    {
        "name": "status",
        "description": "Report what is indexed for this worktree: files, chunks, symbols and files still pending.",
        "inputSchema": {"type": "object", "properties": {**_COMMON, **_TIMEOUT}},
    },
]
_SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}
_JSON_TYPES = {"string": str, "integer": int}


class _BadArguments(ValueError):
    """The caller's arguments do not fit the tool's schema."""


class _Background:
    """Makes vectors for whatever worktree was last asked about, on its own thread and with
    its own engine and database connections, so it never holds up a call. Only started
    when semantic search is on."""

    ROUND_SECONDS = 10     # one stretch of embedding before checking whether to go on
    RETRY_SECONDS = 60     # after the worker was unavailable or another session was embedding

    def __init__(self, default_cwd, dense: bool | None) -> None:
        self._engine = Engine(default_cwd, dense=dense)
        self._wanted: queue.Queue = queue.Queue()
        self._stopping = threading.Event()
        self._thread = threading.Thread(target=self._work, name="aleph-embed", daemon=True)
        self._thread.start()

    def note(self, cwd) -> None:
        """A call was just answered for `cwd`: its chunks may need vectors."""
        self._wanted.put(cwd)

    def stop(self) -> None:
        self._stopping.set()
        self._wanted.put(None)
        self._thread.join(timeout=5)

    def _work(self) -> None:
        while not self._stopping.is_set():
            cwd = self._wanted.get()
            while not self._wanted.empty():  # only the latest matters
                cwd = self._wanted.get()
            while not self._stopping.is_set():
                try:
                    if not self._engine.dense_enabled(cwd):
                        break  # this worktree's language has no verdict in favour of vectors
                    report = self._engine.embed(cwd, budget_seconds=self.ROUND_SECONDS, stop=self._stopping.is_set,
                                                sync_seconds=0.0)  # indexing is the foreground's job
                except Exception:  # noqa: BLE001 - not a repository any more, a locked index: try again on the next call
                    break
                if report.remaining == 0 or not self._wanted.empty():
                    break
                if report.reason and self._stopping.wait(self.RETRY_SECONDS):
                    break


def serve(engine: Engine, incoming: IO[str], outgoing: IO[str]) -> int:
    """Answer requests from `incoming` on `outgoing` until the input closes."""
    background = _Background(engine.default_cwd, engine.dense) if engine.dense is not False else None
    write_lock = threading.Lock()
    state = threading.Lock()      # guards the three names below
    calls: queue.Queue = queue.Queue()
    waiting: set = set()          # ids of calls queued and not yet started
    cancelled: set = set()        # of those, the ones withdrawn before they started
    running: list = [None]

    def send(message: dict) -> None:
        with write_lock:
            try:
                outgoing.write(json.dumps(message) + "\n")  # ASCII: any path can be written
                outgoing.flush()
            except (BrokenPipeError, ValueError):
                pass  # the client has gone; there is nobody to tell

    def work() -> None:
        while True:
            item = calls.get()
            if item is None:
                return
            ident, name, arguments = item
            with state:
                waiting.discard(ident)
                if ident in cancelled:
                    cancelled.discard(ident)
                    continue
                running[0] = ident
            try:
                result = _call(engine, name, arguments)
                if background is not None:
                    background.note(arguments.get("cwd") if isinstance(arguments, dict) else None)
            except Cancelled:
                result = None  # a cancelled request is not answered
            except BaseException as exc:  # noqa: BLE001 - nothing may end this thread: later calls would hang
                result = _failure(name, f"failed unexpectedly: {type(exc).__name__}: {exc}")
            with state:
                running[0] = None
                engine.resume()
            if result is not None:
                send({"jsonrpc": "2.0", "id": ident, "result": result})
            try:
                engine.relieve()  # after the answer has gone out: over its share, the session gives memory back
            except Exception:  # noqa: BLE001 - housekeeping must not end the thread that answers calls
                pass

    worker = threading.Thread(target=work, name="aleph-calls", daemon=True)
    worker.start()
    for line in incoming:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            send(_error(None, -32700, "Parse error: the message is not JSON"))
            continue
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            ident = message.get("id") if isinstance(message, dict) else None
            send(_error(ident if _is_id(ident) else None, -32600, "Invalid request: expected an object with a method"))
            continue
        method, ident, params = message["method"], message.get("id"), message.get("params")
        params = params if isinstance(params, dict) else {}
        if "id" in message and not _is_id(ident):
            send(_error(None, -32600, "Invalid request: an id is a string or a number"))
            continue
        if method == "notifications/cancelled":
            target = params.get("requestId")
            if _is_id(target):
                with state:
                    if running[0] == target:
                        engine.cancel()
                    elif target in waiting:
                        cancelled.add(target)  # a request never made is not remembered
            continue
        if ident is None:
            continue  # any other notification: nothing to answer
        if method == "initialize":
            proposed = params.get("protocolVersion")
            send({"jsonrpc": "2.0", "id": ident, "result": {
                "protocolVersion": proposed if proposed in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": ident, "result": {}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or name not in _SCHEMAS:
                send(_error(ident, -32602, f"Unknown tool: {name}"))
                continue
            with state:
                waiting.add(ident)
            calls.put((ident, name, params.get("arguments")))
        else:
            send(_error(ident, -32601, f"Method not found: {method}"))
    engine.cancel()  # the client is gone: whatever is running has no reader
    calls.put(None)
    worker.join(timeout=5)
    if background is not None:
        background.stop()
    return 0


def _is_id(value) -> bool:
    """A JSON-RPC id: a string or a number. Anything else cannot be echoed or tracked."""
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _failure(name: str, reason: str) -> dict:
    return {"content": [{"type": "text", "text": f"{name} {reason}"}], "isError": True}


def _error(ident, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


def _call(engine: Engine, name: str, arguments: dict) -> dict:
    """Run one tool. A failure of the tool is a result with isError, which the model can
    read and act on; JSON-RPC errors are kept for mistakes in the protocol itself."""
    try:
        text = _run(engine, name, _checked(name, arguments))
    except Cancelled:
        raise
    except gitio.GitTimeout:
        text = "note: stopped at the time limit before the worktree could be read; no answer. Try a longer timeout_ms."
    except (_BadArguments, gitio.GitError, StoreError, outline.OutlineError, ValueError) as exc:
        return _failure(name, f"failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - a defect in the tool must not take the server down
        return _failure(name, f"failed unexpectedly: {type(exc).__name__}: {exc}")
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _checked(name: str, arguments) -> dict:
    schema = _SCHEMAS[name]
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise _BadArguments("arguments must be an object")
    for required in schema.get("required", ()):
        if arguments.get(required) is None:
            raise _BadArguments(f"missing required argument: {required}")
    checked = {}
    for key, value in arguments.items():
        spec = schema["properties"].get(key)
        if spec is None or value is None:
            continue  # an argument we do not know is ignored, not an error
        expected = _JSON_TYPES[spec["type"]]
        if not isinstance(value, expected) or isinstance(value, bool):
            raise _BadArguments(f"argument {key} must be {spec['type']}, not {type(value).__name__}")
        if "enum" in spec and value not in spec["enum"]:
            raise _BadArguments(f"argument {key} must be one of {', '.join(spec['enum'])}, not {value!r}")
        if "minimum" in spec and value < spec["minimum"] or "maximum" in spec and value > spec["maximum"]:
            raise _BadArguments(f"argument {key} is out of range")
        checked[key] = value
    return checked


def _run(engine: Engine, name: str, args: dict) -> str:
    as_json = args.get("format") == "json"
    cwd = args.get("cwd")
    if name == "search":
        answer = engine.search(args["query"], k=args.get("k", 8), kind=args.get("kind", "any"),
                               path_glob=args.get("path_glob"), lang=args.get("lang"), cwd=cwd,
                               timeout_ms=args.get("timeout_ms"))
        if as_json:
            return json.dumps(render.as_json(answer))
        return engine.render(answer, detail=args.get("detail", "snippets"))
    if name == "symbol":
        found = engine.symbol(args["name"], k=args.get("k", 20), cwd=cwd, timeout_ms=args.get("timeout_ms"))
        if as_json:
            return json.dumps(asdict(found))
        lines = [f"{hit.path}:{hit.line}  {hit.kind}  {hit.qualified}\n  {hit.signature}" for hit in found.symbols]
        if not lines:
            lines = [f'no definition named "{found.name}" in this worktree']
        if found.pending:
            lines.append(f"note: {found.pending} files are not indexed yet; results may be incomplete")
        return "\n".join(lines)
    if name == "outline":
        result = engine.outline(args["path"], cwd=cwd, timeout_ms=args.get("timeout_ms"))
        return json.dumps(asdict(result)) if as_json else outline.text(result)
    status = engine.status(cwd=cwd, timeout_ms=args.get("timeout_ms"))
    return json.dumps(status) if as_json else "\n".join(f"{key}: {value}" for key, value in status.items())


def main(engine: Engine) -> int:
    """Serve on the process's standard streams. Anything else that prints goes to stderr,
    so a stray message cannot corrupt the protocol."""
    outgoing = sys.stdout
    sys.stdout = sys.stderr
    try:
        return serve(engine, sys.stdin, outgoing)
    finally:
        sys.stdout = outgoing
