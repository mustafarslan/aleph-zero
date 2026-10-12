"""The session's own memory: reading it cheaply, and giving some back when over its share."""
import subprocess
import time

from aleph_zero import memstat
from aleph_zero.engine import Engine


def test_this_process_reads_its_own_private_memory_without_starting_another(monkeypatch):
    def no_processes(*args, **kwargs):
        raise AssertionError("a process was started to read this process's memory")

    monkeypatch.setattr(subprocess, "run", no_processes)
    monkeypatch.setattr(subprocess, "Popen", no_processes)
    first = memstat.private_bytes()
    assert 5e6 < first < 2e9
    ballast = bytearray(40_000_000)
    for offset in range(0, len(ballast), 4096):
        ballast[offset] = 1                      # touched, so it is really ours
    assert memstat.private_bytes() - first > 30e6
    started = time.perf_counter()
    for _ in range(200):
        memstat.private_bytes()
    assert (time.perf_counter() - started) / 200 < 0.001   # cheap enough to do after every call
    del ballast


def test_under_its_limit_a_session_releases_nothing(repo, monkeypatch):
    from aleph_zero import engine as engine_module

    assert engine_module.SESSION_MEMORY_MB == 130
    monkeypatch.setenv("ALEPH_SESSION_MEMORY_MB", "100000")   # the test process itself is larger than a session
    engine = Engine(repo.root)
    engine.search("refresh token")
    assert engine.relieve() is None
    memory = engine.status()["memory"]
    assert memory["releases"] == 0 and 5 < memory["private_mb"] < memory["limit_mb"] == 100000


def test_over_its_limit_a_session_gives_back_what_it_can_rebuild_and_still_answers(repo, tmp_path, monkeypatch):
    other = tmp_path / "second-worktree"
    repo.git("worktree", "add", "-q", str(other))
    engine = Engine(repo.root)
    assert engine.search("refresh token", cwd=other).hits
    assert engine.search("refresh token").hits
    assert len(engine._indexes) == 2
    monkeypatch.setenv("ALEPH_SESSION_MEMORY_MB", "1")
    report = engine.relieve()
    assert report["limit_mb"] == 1 and report["before_mb"] > 1
    assert len(engine._indexes) == 1                       # the worktree not in use was let go
    assert [hit.path for hit in engine.search("render invoice").hits][0] == "src/billing/invoice.py"
    assert engine.search("refresh token", cwd=other).hits  # and comes back when asked for
    assert engine.status()["memory"]["releases"] >= 1


def test_the_server_checks_its_memory_after_a_call(repo, monkeypatch):
    import json

    from test_server import Client

    monkeypatch.setenv("ALEPH_SESSION_MEMORY_MB", "1")
    client = Client(repo.root)
    try:
        client.start()
        assert "token_store.py" in client.text("search", query="refresh token")
        assert "token_store.py" in client.text("search", query="refresh token")
        status = json.loads(client.text("status", format="json"))
        assert status["memory"]["releases"] >= 2 and status["memory"]["limit_mb"] == 1
    finally:
        client.close()


def test_the_background_embedding_thread_lets_go_of_its_connections_when_it_has_nothing_to_do(repo, tmp_path):
    """It has its own engine. Without this it keeps a connection, with its page cache, for
    every worktree the session was ever asked about, where the memory check cannot reach."""
    import time

    from aleph_zero.server import _Background

    other = tmp_path / "second-worktree"
    repo.git("worktree", "add", "-q", str(other))
    _Background.IDLE_SECONDS, usual = 0.2, _Background.IDLE_SECONDS
    background = _Background(repo.root, dense=False)
    _Background.IDLE_SECONDS = usual
    background.IDLE_SECONDS = 0.2
    try:
        background.note(str(repo.root))
        background.note(str(other))
        deadline = time.time() + 10
        while time.time() < deadline and (background.rounds < 1 or background._engine._indexes):
            time.sleep(0.05)
        assert background.rounds >= 1 and background._engine._indexes == {}
    finally:
        background.stop()
