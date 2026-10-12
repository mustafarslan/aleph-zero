import json
import select
import subprocess
import sys
import time

import pytest

from aleph_zero import server

LATEST = "2025-11-25"


class Client:
    """Speaks newline-delimited JSON-RPC to `aleph serve` running as a child process."""

    def __init__(self, cwd):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "aleph_zero.cli", "--cwd", str(cwd), "serve"],
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


def test_the_modern_handshake_is_declined_so_the_client_falls_back(client):
    # Claude Code 2.1.296 first sends server/discover and, on "method not found", sends initialize.
    client.send({"jsonrpc": "2.0", "id": "server-discover-probe-1", "method": "server/discover", "params": {}})
    reply = client.read()
    assert reply["id"] == "server-discover-probe-1" and reply["error"]["code"] == -32601
    assert client.start()["protocolVersion"] == LATEST


def test_four_tools_are_listed_and_search_is_always_loaded(client):
    client.start()
    tools = {tool["name"]: tool for tool in client.request("tools/list")["result"]["tools"]}
    assert set(tools) == {"search", "outline", "symbol", "status"}
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
    assert outside["isError"] is True and "not a git repository" in outside["content"][0]["text"]
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
    assert sorted(names) == ["outline", "search", "status", "symbol"]
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
