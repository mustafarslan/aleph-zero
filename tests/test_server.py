import json
import os
import select
import subprocess
import sys
import time

import pytest

from aleph_zero import server

LATEST = "2025-11-25"


class Client:
    """Speaks newline-delimited JSON-RPC to `aleph serve` running as a child process."""

    def __init__(self, cwd, env=None):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "aleph_zero.cli", "--cwd", str(cwd), "serve"],
            env={**os.environ, **(env or {})},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.next_id = 0

    def send(self, payload):
        self.proc.stdin.write((payload if isinstance(payload, str) else json.dumps(payload)) + "\n")
        self.proc.stdin.flush()

    def read(self, seconds=15):
        ready, _, _ = select.select([self.proc.stdout], [], [], seconds)
        assert ready, f"no answer from the server within {seconds} s"
        line = self.proc.stdout.readline()
        assert line, "the server closed its output: " + self.proc.stderr.read()[-400:]
        return json.loads(line)

    def request(self, method, params=None):
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, **({"params": params} if params is not None else {})})
        reply = self.read()
        assert reply["id"] == self.next_id
        return reply

    def start(self, version=LATEST):
        reply = self.request("initialize", {"protocolVersion": version, "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply["result"]

    def call(self, tool, /, **arguments):
        return self.request("tools/call", {"name": tool, "arguments": arguments})

    def text(self, tool, /, **arguments):
        result = self.call(tool, **arguments)["result"]
        assert result["isError"] is False, result
        return result["content"][0]["text"]

    def close(self):
        self.proc.stdin.close()
        try:
            code = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


@pytest.fixture
def client(repo):
    client = Client(repo.root)
    yield client
    if client.proc.poll() is None:
        client.close()


def test_initialize_negotiates_the_protocol_version(client):
    started = client.start("2025-06-18")
    assert started["protocolVersion"] == "2025-06-18"  # a version we speak: the client's choice
    assert started["serverInfo"]["name"] == "aleph-zero" and "tools" in started["capabilities"]
    assert 200 < len(started["instructions"]) <= 2048
    newer = client.request("initialize", {"protocolVersion": "2099-01-01", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "0"}})
    assert newer["result"]["protocolVersion"] == LATEST  # one we do not know: our latest


MODERN = "2026-07-28"
VERSION, SERVER = "io.modelcontextprotocol/protocolVersion", "io.modelcontextprotocol/serverInfo"


@pytest.fixture
def handshake_only(repo):
    found = Client(repo.root, env={"ALEPH_MCP_STATELESS": "0"})
    yield found
    found.close()


def test_switched_off_the_newer_revision_is_declined_and_the_client_falls_back(handshake_only):
    # The way back. Claude Code 2.1.296 first sends server/discover and, on "method not found", sends initialize.
    client = handshake_only
    for params in ({}, {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}):       # as it was seen to ask, and in full
        client.send({"jsonrpc": "2.0", "id": "server-discover-probe-1", "method": "server/discover", "params": params})
        reply = client.read()
        assert reply["id"] == "server-discover-probe-1" and reply["error"]["code"] == -32601
    assert client.start()["protocolVersion"] == LATEST
    assert set(client.request("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}})["result"]) == {"tools"}


@pytest.fixture
def modern(repo):
    found = Client(repo.root)     # nothing set: the revision without a handshake is what a session gets
    yield found
    found.close()


def test_the_revision_without_a_handshake_is_spoken_request_by_request(modern):
    """2026-07-28: no `initialize`. Each request says its protocol version; `server/discover` says what the server speaks."""
    client = modern
    meta = {"_meta": {VERSION: MODERN, "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
                      "io.modelcontextprotocol/clientCapabilities": {}}}
    found = client.request("server/discover", meta)["result"]
    assert found["supportedVersions"][0] == MODERN and LATEST in found["supportedVersions"]      # the older ones too
    assert "tools" in found["capabilities"] and 200 < len(found["instructions"]) <= 2048
    assert found["resultType"] == "complete" and found["_meta"][SERVER]["name"] == "aleph-zero"
    listed = client.request("tools/list", meta)["result"]                                          # with no handshake before it
    assert {tool["name"] for tool in listed["tools"]} >= {"search", "symbol"}
    assert listed["resultType"] == "complete" and listed["ttlMs"] > 0 and listed["cacheScope"] in ("public", "private")
    called = client.request("tools/call", {"name": "status", "arguments": {}, **meta})["result"]
    assert called["isError"] is False and called["resultType"] == "complete" and called["_meta"][SERVER]["name"] == "aleph-zero"


def test_a_version_the_server_does_not_speak_is_refused_with_the_ones_it_does(modern):
    client = modern
    unknown = {"_meta": {VERSION: "2099-01-01"}}
    for method, params in (("server/discover", unknown), ("tools/list", unknown), ("tools/call", {"name": "status", "arguments": {}, **unknown})):
        error = client.request(method, params)["error"]
        assert error["code"] == -32022 and error["data"]["requested"] == "2099-01-01"
        assert MODERN in error["data"]["supported"] and LATEST in error["data"]["supported"]


def test_a_question_about_what_is_spoken_is_answered_even_when_it_names_no_revision(modern):
    found = modern.request("server/discover", {})["result"]                       # the bare form a client may probe with
    assert found["supportedVersions"][0] == MODERN and found["resultType"] == "complete"
    older = modern.request("initialize", {"protocolVersion": LATEST, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"},
                                          "_meta": {VERSION: LATEST}})
    assert older["result"]["protocolVersion"] == LATEST                           # a revision it speaks is not "unsupported"
    assert set(modern.request("tools/list", {"_meta": {VERSION: LATEST}})["result"]) == {"tools"}


def test_a_client_of_the_handshake_revisions_is_answered_as_before(modern):
    client = modern
    started = client.start()
    assert started["protocolVersion"] == LATEST
    listed = client.request("tools/list")["result"]
    assert set(listed) == {"tools"}                                   # nothing of the newer revision is put in its way
    assert set(client.call("status")["result"]) == {"content", "isError"}
    client.send({"jsonrpc": "2.0", "id": "p", "method": "ping"})
    assert client.read() == {"jsonrpc": "2.0", "id": "p", "result": {}}


def test_the_sdk_s_client_of_the_new_revision_can_discover_list_and_call(repo):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable, env={**os.environ},
                                       args=["-m", "aleph_zero.cli", "--cwd", str(repo.root), "serve"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                found = await session.discover()
                tools = await session.list_tools()
                result = await session.call_tool("search", {"query": "render_invoice"})
                return found, session.protocol_version, [tool.name for tool in tools.tools], result

    found, version, names, result = anyio.run(run)
    assert MODERN in found.supported_versions and version == MODERN
    assert "search" in names and result.is_error is False and "src/billing/invoice.py:1-3" in result.content[0].text


def test_six_tools_are_listed_and_search_is_always_loaded(client):
    client.start()
    tools = {tool["name"]: tool for tool in client.request("tools/list")["result"]["tools"]}
    assert set(tools) == {"search", "outline", "symbol", "status", "recall", "remember"}
    assert tools["search"]["_meta"] == {"anthropic/alwaysLoad": True}
    assert all("_meta" not in tool for name, tool in tools.items() if name != "search")
    assert sum(len(tool["description"]) for tool in tools.values()) <= 1200
    assert tools["search"]["inputSchema"]["required"] == ["query"]
    assert set(tools["search"]["inputSchema"]["properties"]) == {
        "query", "k", "kind", "path_glob", "lang", "detail", "format", "cwd", "timeout_ms"}
    for tool in tools.values():
        assert tool["inputSchema"]["type"] == "object" and {"format", "cwd"} <= set(tool["inputSchema"]["properties"])


def test_each_tool_answers_in_text_and_in_json(client, repo):
    client.start()
    found = client.text("search", query="render_invoice")
    assert found.startswith("src/billing/invoice.py:1-3  function  render_invoice")
    as_json = json.loads(client.text("search", query="render_invoice", format="json"))
    assert as_json["hits"][0]["path"] == "src/billing/invoice.py" and as_json["pending"] == 0
    assert client.text("symbol", name="refresh_token").startswith("src/auth/token_store.py:4  function  refresh_token")
    assert json.loads(client.text("symbol", name="refresh_token", format="json"))["symbols"][0]["line"] == 4
    assert client.text("outline", path="docs/guide.md").startswith("docs/guide.md  markdown  ")
    assert json.loads(client.text("outline", path="docs/guide.md", format="json"))["items"][0]["name"] == "Guide"
    status = client.text("status")
    assert f"root: {repo.root}" in status and "pending: 0" in status
    assert json.loads(client.text("status", format="json"))["files_in_view"] == 3


def test_search_options_reach_the_engine(client, repo):
    client.start()
    assert "docs/guide.md" in client.text("search", query="invoices", kind="docs")
    assert "src/" not in client.text("search", query="invoices", kind="docs")
    assert client.text("search", query="token", path_glob="docs/*", detail="locations").count("\n") < 6
    assert "markdown" not in client.text("search", query="token", lang="python", detail="locations")


def test_cwd_selects_another_worktree(client, repo, tmp_path):
    other = tmp_path / "feature-tree"
    repo.git("worktree", "add", "-q", "-b", "feature", str(other))
    (other / "src/feature_only.py").write_text("def quokka_feature():\n    return 1\n")
    client.start()
    assert "src/feature_only.py" in client.text("search", query="quokka_feature", cwd=str(other))
    assert client.text("search", query="quokka_feature").startswith("no results")


def test_a_tool_that_fails_says_so_in_its_result_not_as_a_protocol_error(client, tmp_path):
    client.start()
    missing = client.call("outline", path="src/nope.py")["result"]
    assert missing["isError"] is True and "not a file in this worktree" in missing["content"][0]["text"]
    outside = client.call("search", query="x", cwd=str(tmp_path))["result"]
    assert outside["isError"] is True and "is not a git repository" in outside["content"][0]["text"]     # a folder that only holds one
    bad = client.call("search", query="x", kind="everything")["result"]
    assert bad["isError"] is True and "kind" in bad["content"][0]["text"]
    no_query = client.call("search")["result"]
    assert no_query["isError"] is True and "query" in no_query["content"][0]["text"]
    wrong_type = client.call("search", query="x", k="many")["result"]
    assert wrong_type["isError"] is True and "k" in wrong_type["content"][0]["text"]


def test_protocol_mistakes_get_json_rpc_errors_and_the_server_keeps_running(client):
    client.start()
    assert client.request("tools/call", {"name": "nonexistent", "arguments": {}})["error"]["code"] == -32602
    assert client.request("resources/list")["error"]["code"] == -32601
    client.send("{this is not json")
    broken = client.read()
    assert broken["id"] is None and broken["error"]["code"] == -32700
    client.send(json.dumps([1, 2, 3]))
    assert client.read()["error"]["code"] == -32600
    client.send({"jsonrpc": "2.0", "method": "notifications/some/unknown"})  # ignored
    assert client.request("ping")["result"] == {}


def test_a_cancelled_call_stops_and_gets_no_answer(client, repo):
    for i in range(500):
        repo.write(f"gen/module_{i}.py", "".join(f"def function_{i}_{j}():\n    return {j}\n\n\n" for j in range(30)))
    repo.commit("many files")
    client.start()
    client.send({"jsonrpc": "2.0", "id": 900, "method": "tools/call",
                 "params": {"name": "search", "arguments": {"query": "function_499_29"}}})  # a cold index: seconds
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 900}})
    started = time.monotonic()
    assert client.request("ping")["result"] == {}  # answered at once, and nothing came for 900 before it
    assert time.monotonic() - started < 1.0
    again = client.text("search", query="function_499_29")
    assert "gen/module_499.py" in again


def test_the_server_exits_when_its_input_closes(client):
    client.start()
    client.text("status")
    assert client.close() == 0


def test_the_server_module_needs_no_sdk():
    code = "import sys, aleph_zero.server; sys.exit(int('mcp' in sys.modules or 'pydantic' in sys.modules))"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
    assert len(server.INSTRUCTIONS) <= 2048
    assert len(server.INSTRUCTIONS + server.GRAPH_INSTRUCTIONS) <= 2048


def test_the_sdk_client_can_list_and_call_our_tools(repo):
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def run():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "aleph_zero.cli", "--cwd", str(repo.root), "serve"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("search", {"query": "render_invoice"})
                return [tool.name for tool in tools.tools], result

    names, result = anyio.run(run)
    assert sorted(names) == ["outline", "recall", "remember", "search", "status", "symbol"]
    assert result.is_error is False and "src/billing/invoice.py:1-3" in result.content[0].text


def test_ids_of_a_wrong_type_are_refused_and_the_server_lives(client):
    client.start()
    client.send({"jsonrpc": "2.0", "id": {"a": 1}, "method": "tools/call", "params": {"name": "status", "arguments": {}}})
    assert client.read()["error"]["code"] == -32600
    client.send({"jsonrpc": "2.0", "id": True, "method": "ping"})
    assert client.read()["error"]["code"] == -32600
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": [1]}})
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": "nonsense"})
    assert "root:" in client.text("status")          # tool calls still run
    client.send({"jsonrpc": "2.0", "id": "text-id", "method": "ping"})
    assert client.read() == {"jsonrpc": "2.0", "id": "text-id", "result": {}}


def test_a_cancellation_for_a_request_never_made_does_not_swallow_a_later_one(client):
    client.start()
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
    client.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "status", "arguments": {}}})
    reply = client.read()
    assert reply["id"] == 7 and reply["result"]["isError"] is False


def test_arguments_that_are_null_or_absurd_are_argument_errors(client):
    client.start()
    for arguments, word in (({"query": None}, "query"), ({"query": "x", "timeout_ms": 10**30}, "timeout_ms"),
                            ({"query": "x", "k": 0}, "k")):
        result = client.request("tools/call", {"name": "search", "arguments": arguments})["result"]
        assert result["isError"] is True and word in result["content"][0]["text"]
        assert "unexpectedly" not in result["content"][0]["text"]
    not_an_object = client.request("tools/call", {"name": "search", "arguments": ["x"]})["result"]
    assert not_an_object["isError"] is True


def test_only_protocol_versions_without_batching_are_offered(client):
    assert client.start("2024-11-05")["protocolVersion"] == LATEST
    assert server.PROTOCOL_VERSIONS == ("2025-11-25", "2025-06-18")


def test_a_time_limit_shorter_than_git_needs_is_a_marked_answer_not_an_error(client, repo):
    client.start()
    result = client.call("search", query="render_invoice", timeout_ms=1)["result"]
    assert result["isError"] is False and "time limit" in result["content"][0]["text"]
    assert "render_invoice" in client.text("search", query="render_invoice")
    for tool, arguments in (("outline", {"path": "docs/guide.md"}), ("status", {})):
        slow = client.call(tool, timeout_ms=1, **arguments)["result"]
        assert slow["isError"] is False and "time limit" in slow["content"][0]["text"]


def test_a_query_with_no_results_does_not_echo_five_thousand_words(client):
    from aleph_zero.chunking import estimate_tokens

    client.start()
    text = client.text("search", query=" ".join(f"zzqword{i}" for i in range(5000)))
    assert text.startswith('no results for "zzqword0 ') and estimate_tokens(text) < 100


def test_with_semantic_search_on_the_server_makes_vectors_in_the_background(repo):
    import os
    import shutil
    import tempfile
    from pathlib import Path

    cache = Path(tempfile.mkdtemp(prefix="az-", dir="/tmp"))
    home = cache / "aleph-zero" / "worker"
    (home / "bin").mkdir(parents=True)
    (home / "models").mkdir()
    fake = Path(__file__).parent / "fake_worker.py"
    (home / "bin" / "llama-server").write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
    (home / "bin" / "llama-server").chmod(0o755)
    (home / "models" / "embeddinggemma-2-Q8_0.gguf").write_bytes(b"not a real model")
    env = {**os.environ, "XDG_CACHE_HOME": str(cache), "ALEPH_DENSE": "1", "ALEPH_WORKER_POLL_SECONDS": "0.2",
           "ALEPH_WORKER_IDLE_EXIT_SECONDS": "4", "ALEPH_WORKER_LOAD_SECONDS": "10"}
    client = Client.__new__(Client)
    client.proc = subprocess.Popen([sys.executable, "-m", "aleph_zero.cli", "--cwd", str(repo.root), "serve"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    client.next_id = 0
    try:
        client.start()
        first = json.loads(client.text("search", query="render_invoice", format="json"))
        assert first["hits"][0]["path"] == "src/billing/invoice.py"      # answered at once, before any vector exists
        assert first["dense"].startswith("no chunk of this worktree has a vector yet")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            vectors = json.loads(client.text("status", format="json"))["vectors"]
            if vectors["chunks"] and vectors["with_vector"] == vectors["chunks"]:
                break
            time.sleep(0.2)
        assert vectors["with_vector"] == vectors["chunks"] > 0 and vectors["enabled"] is True
        assert json.loads(client.text("search", query="render_invoice", format="json"))["dense"] == "used"
        repo.write("src/new_module.py", "def freshly_added():\n    return 1\n")   # a new chunk gets its vector too
        client.text("search", query="freshly_added")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            vectors = json.loads(client.text("status", format="json"))["vectors"]
            if vectors["with_vector"] == vectors["chunks"]:
                break
            time.sleep(0.2)
        assert vectors["with_vector"] == vectors["chunks"]
        assert client.close() == 0
    finally:
        if client.proc.poll() is None:
            client.proc.kill()
        subprocess.run([sys.executable, "-m", "aleph_zero.cli", "embed", "stop"], env=env, capture_output=True)
        shutil.rmtree(cache, ignore_errors=True)


def test_the_graph_tool_is_listed_and_answers_only_when_switched_on(repo):
    """M7 is built ahead of its gate: the agent is not offered the tool unless ALEPH_GRAPH=1."""
    repo.write("src/billing/use.py", "from .invoice import render_invoice\n\ndef bill():\n    return render_invoice(1)\n")
    off = Client(repo.root)
    try:
        off.start()
        assert "references" not in {tool["name"] for tool in off.request("tools/list")["result"]["tools"]}
        assert off.call("references", name="render_invoice")["error"]["message"] == "Unknown tool: references"
    finally:
        off.close()
    on = Client(repo.root, env={"ALEPH_GRAPH": "1"})
    try:
        on.start()
        tools = {tool["name"]: tool for tool in on.request("tools/list")["result"]["tools"]}
        assert tools["references"]["inputSchema"]["required"] == ["name"] and "_meta" not in tools["references"]
        text = on.text("references", name="render_invoice")
        assert "src/billing/use.py:4  in bill  render_invoice()" in text
        as_json = json.loads(on.text("references", name="render_invoice", direction="callers", format="json"))
        assert as_json["callers"][0]["certain"] is True and as_json["graph_pending"] == 0
        repo.write("docs/adr/0003-invoices-are-rendered-server-side.md", "# 3. Invoices are rendered on the server\n\n## Status\n\nAccepted\n")
        assert "required" not in tools["decisions"]["inputSchema"] and "path" in tools["decisions"]["inputSchema"]["properties"]
        assert on.text("decisions", path="src/billing/use.py") == "src/billing/use.py: no decision record names it in its applies-to"
        assert on.text("decisions", task="render the invoice in the browser instead").startswith(
            "docs/adr/0003-invoices-are-rendered-server-side.md  ADR 3  Invoices are rendered on the server  [Accepted]")
        repo.write("src/api.py", "@app.get(\"/invoices/{invoice_id}\")\ndef show(invoice_id): ...\n")
        assert "kind" in tools["facts"]["inputSchema"]["properties"] and "facts" not in {tool["name"] for tool in server.TOOLS}
        assert on.text("facts").startswith("route  1")
        assert on.text("facts", kind="route") == "GET /invoices/{invoice_id}  src/api.py:1  fastapi"
    finally:
        on.close()


def test_a_session_s_first_search_has_vectors_because_the_server_wakes_the_worker_when_it_starts(repo):
    # The worker used to be started only after a first call had been answered, so a session's first question, which
    # is the plugin's own and most of what an agent ever asks, was answered by words alone. Measured with the real
    # worker: the first two searches of a session without vectors.
    import os
    import shutil
    import tempfile
    from pathlib import Path

    cache = Path(tempfile.mkdtemp(prefix="az-", dir="/tmp"))
    home = cache / "aleph-zero" / "worker"
    (home / "bin").mkdir(parents=True)
    (home / "models").mkdir()
    fake = Path(__file__).parent / "fake_worker.py"
    (home / "bin" / "llama-server").write_text(f"#!/bin/sh\nexec {sys.executable} {fake} \"$@\"\n")
    (home / "bin" / "llama-server").chmod(0o755)
    (home / "models" / "embeddinggemma-2-Q8_0.gguf").write_bytes(b"not a real model")
    env = {**os.environ, "XDG_CACHE_HOME": str(cache), "ALEPH_DENSE": "1", "ALEPH_WORKER_POLL_SECONDS": "0.2",
           "ALEPH_WORKER_IDLE_EXIT_SECONDS": "60", "ALEPH_WORKER_LOAD_SECONDS": "10"}
    aleph = [sys.executable, "-m", "aleph_zero.cli", "--cwd", str(repo.root)]
    run = lambda *args: subprocess.run([*aleph, *args], capture_output=True, text=True, env=env, timeout=120)  # noqa: E731

    def session() -> Client:
        client = Client.__new__(Client)
        client.proc = subprocess.Popen([*aleph, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        client.next_id = 0
        return client

    try:
        assert run("embed", "run").returncode == 0                       # every chunk has its vector
        assert "stopped" in run("embed", "stop").stdout and "running: no" in run("embed", "status").stdout
        client = session()
        try:
            client.start()
            deadline = time.monotonic() + 20                              # no call is made: the server does this by itself
            while time.monotonic() < deadline and "running: yes" not in run("embed", "status").stdout:
                time.sleep(0.2)
            assert "running: yes" in run("embed", "status").stdout
            # Running is not awake: a worker that sleeps has unloaded its model and takes longer to wake than a
            # question waits. The server has asked it for one vector, so the model is loaded.
            decoded = "from aleph_zero.embed import supervisor, worker; from aleph_zero.embed.client import EmbedClient; " \
                      "print(supervisor._requests_served(EmbedClient(worker.socket_path())))"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (asked := subprocess.run([sys.executable, "-c", decoded], capture_output=True, text=True, env=env).stdout.strip()) in ("", "0", "None"):
                time.sleep(0.2)
            assert asked not in ("", "0", "None")
            first = json.loads(client.text("search", query="render_invoice", format="json"))
            assert first["dense"] == "used" and first["hits"][0]["path"] == "src/billing/invoice.py"
            assert client.close() == 0
        finally:
            if client.proc.poll() is None:
                client.proc.kill()
    finally:
        run("embed", "stop")
        shutil.rmtree(cache, ignore_errors=True)


def test_the_background_thread_works_only_when_a_vector_is_missing_or_the_worker_is_not_there(repo, fake_worker, monkeypatch):
    # It has an engine of its own and built its own view of the worktree after every answer: on a repository of
    # 42,000 files that was 70 MB of the session and a slower next question, for nothing once every chunk has a vector.
    from aleph_zero.embed import worker
    from aleph_zero.engine import Engine

    monkeypatch.setattr(worker, "available", lambda wait=2.0: fake_worker)
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    monkeypatch.setattr(worker, "ensure", lambda: fake_worker)
    Engine(repo.root, dense=True).embed()                         # every chunk has its vector
    noted: list[object] = []

    class Watched(server._Background):
        def note(self, cwd) -> None:
            noted.append("note")
            super().note(cwd)

        def wake(self) -> None:
            noted.append("wake")
            super().wake()

    monkeypatch.setattr(server, "_Background", Watched)

    def session(*queries):
        # The input stays open until every answer has come, as a client's does: an input that ends is a client that left.
        import os
        import threading

        into_read, into_write = os.pipe()
        out_read, out_write = os.pipe()
        incoming, outgoing = os.fdopen(into_read, "r"), os.fdopen(out_write, "w")
        running = threading.Thread(target=server.serve, args=(Engine(repo.root, dense=True), incoming, outgoing))
        running.start()
        writer, reader = os.fdopen(into_write, "w"), os.fdopen(out_read, "r")
        answers = []
        for n, query in enumerate(queries, 1):
            writer.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": "tools/call",
                                     "params": {"name": "search", "arguments": {"query": query}}}) + "\n")
            writer.flush()
            answers.append(json.loads(reader.readline()))
        time.sleep(0.5)                                           # what the session does after its answers, before it ends
        writer.close()
        running.join(timeout=10)
        outgoing.close()
        reader.close()
        return answers

    answers = session("render_invoice", "refresh token", "how is an invoice rendered")
    assert len(answers) == 3 and noted == ["wake"]                # woken at the start, and nothing more to do

    noted.clear()
    repo.write("src/billing/credit.py", "def credit_note(order):\n    return -order.total\n")   # a chunk without a vector
    session("credit_note")
    assert noted[0] == "wake" and "note" in noted                 # it goes to work for that chunk

    noted.clear()
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker.parent / "gone.sock")       # the worker has exited
    session("render_invoice")
    assert "note" in noted                                        # and starts it again, as before


def test_the_graph_switch_takes_the_plugin_option_s_words_as_well(monkeypatch):
    # The plugin passes its `graph` option (on/off, like its other switches) through to the engine as ALEPH_GRAPH.
    from aleph_zero import server
    for value, enabled in (("1", True), ("on", True), ("off", False), ("0", False), ("", False),
                           ("true", True), ("ON", True), ("yes", True), ("false", False), ("no", False)):
        monkeypatch.setenv("ALEPH_GRAPH", value)
        assert server.graph_enabled() is enabled, value


def test_the_status_from_the_server_says_whether_vectors_are_being_made_now(repo, monkeypatch):
    # The plugin's status line reads this: making, waiting (and why) or idle, and the rate of the last round.
    from types import SimpleNamespace

    from aleph_zero.engine import Engine

    monkeypatch.setattr(server, "_BACKGROUND", SimpleNamespace(state="waiting", reason="the worker did not answer", rate=88.5))
    status = json.loads(server._run(Engine(repo.root), "status", {"format": "json"}))
    assert status["vectors"]["making"] == {"state": "waiting", "reason": "the worker did not answer", "chunks_per_second": 88.5}
    monkeypatch.setattr(server, "_BACKGROUND", None)
    assert "making" not in json.loads(server._run(Engine(repo.root), "status", {"format": "json"}))["vectors"]


def test_a_status_that_finds_vectors_missing_wakes_the_server_s_background_as_a_search_does(repo, monkeypatch):
    from aleph_zero.engine import Engine

    engine = Engine(repo.root, dense=True)
    engine.search("refresh token")                          # indexed; vectors are wanted, none made (no worker here)
    engine._vectors_wanted.clear()                          # as after a session that started with everything made
    status = engine.status()
    assert status["vectors"]["with_vector"] < status["vectors"]["chunks"]
    assert engine.vectors_wanted() is True
