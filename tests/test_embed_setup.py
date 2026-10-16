import hashlib
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from aleph_zero import cli, doctor
from aleph_zero.embed import setup as embed_setup
from aleph_zero.embed import worker

CONTENT = b"GGUF" + bytes(range(256)) * 400  # stands in for the model file


@pytest.fixture
def home(monkeypatch):
    cache = Path(tempfile.mkdtemp(prefix="az-", dir="/tmp"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setattr(embed_setup, "MODEL_SHA256", hashlib.sha256(CONTENT).hexdigest())
    yield worker.home()
    shutil.rmtree(cache, ignore_errors=True)


@pytest.fixture
def served(monkeypatch):
    """A local web server for the model file that honours Range, and records what was asked."""
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            start = 0
            if self.headers.get("Range"):
                start = int(self.headers["Range"].removeprefix("bytes=").rstrip("-"))
            asked.append(start)
            if start >= len(CONTENT):                                # as a real server answers a range past the end
                self.send_response(416)
                self.end_headers()
                return
            body = CONTENT[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(embed_setup, "MODEL_URL", f"http://127.0.0.1:{server.server_port}/model.gguf")
    yield asked
    server.shutdown()


def test_the_model_is_downloaded_and_checked(home, served):
    path = embed_setup.install_model()
    assert path == worker.model() and path.read_bytes() == CONTENT and served == [0]
    assert embed_setup.install_model() == path and served == [0]  # already there and correct: nothing fetched


def test_an_interrupted_download_resumes_where_it_stopped(home, served):
    part = worker.model().with_suffix(".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(CONTENT[:40_000])
    assert embed_setup.install_model().read_bytes() == CONTENT
    assert served == [40_000] and not part.exists()


def test_a_file_with_the_wrong_checksum_is_refused_and_removed(home, served, monkeypatch):
    monkeypatch.setattr(embed_setup, "MODEL_SHA256", "0" * 64)
    with pytest.raises(embed_setup.SetupError, match="checksum"):
        embed_setup.install_model()
    assert not worker.model().exists() and not worker.model().with_suffix(".part").exists()


def test_a_local_copy_of_the_model_is_used_without_the_network(home, served, tmp_path):
    local = tmp_path / "model.gguf"
    local.write_bytes(CONTENT)
    assert embed_setup.install_model(local).read_bytes() == CONTENT and served == []
    (tmp_path / "wrong.gguf").write_bytes(b"something else")
    worker.model().unlink()
    with pytest.raises(embed_setup.SetupError, match="checksum"):
        embed_setup.install_model(tmp_path / "wrong.gguf")


def test_the_build_is_static_and_pinned_with_no_patch_of_its_own(home):
    steps = embed_setup.build_steps(Path("/src/llama.cpp"), Path("/build"), metal=True)
    joined = [" ".join(step) for step in steps]
    assert any(f"checkout --quiet --force {embed_setup.LLAMA_COMMIT}" in step for step in joined)
    assert not any(" apply " in step for step in joined)                 # upstream since PR #30255 (DEFERRED 4)
    configure = next(step for step in joined if step.startswith("cmake -S"))
    assert "-DBUILD_SHARED_LIBS=OFF" in configure and "-DGGML_METAL=ON" in configure and "-DLLAMA_CURL=OFF" in configure
    assert any("--target llama-server" in step for step in joined)
    assert "-DGGML_METAL=OFF" in " ".join(" ".join(s) for s in embed_setup.build_steps(Path("/s"), Path("/b"), metal=False))


def test_doctor_and_status_say_whether_the_worker_is_installed(home, capsys):
    row = next(row for row in doctor.check(Path("/")) if row.name == "embedding worker")
    assert row.level == "note" and "aleph embed setup" in row.detail
    (home / "bin").mkdir()
    (home / "bin" / "llama-server").write_text("#!/bin/sh\n")
    worker.model().parent.mkdir(parents=True, exist_ok=True)
    worker.model().write_bytes(CONTENT)
    row = next(row for row in doctor.check(Path("/")) if row.name == "embedding worker")
    assert row.level == "ok" and "not running" in row.detail
    assert cli.main(["embed", "status"]) == 0
    out = capsys.readouterr().out
    assert "installed: yes" in out and "running: no" in out


def test_only_the_pinned_commit_of_llama_cpp_is_fetched_not_its_history(home, monkeypatch):
    # A full clone is hundreds of megabytes of history for one commit that is built.
    ran = []
    monkeypatch.setattr(embed_setup, "_run", lambda command: ran.append([str(part) for part in command]))
    monkeypatch.setattr(embed_setup.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    with pytest.raises(embed_setup.SetupError):          # nothing is built here, so no server comes out
        embed_setup.install_binary()
    fetches = [command for command in ran if "fetch" in command]
    assert fetches and "--depth" in fetches[0] and fetches[0][-1] == embed_setup.LLAMA_COMMIT
    assert not any(command[:2] == ["git", "clone"] for command in ran)


def test_a_fetch_that_was_interrupted_is_made_again_on_the_next_try(home, monkeypatch):
    # `git init` leaves the directory behind when the fetch after it fails; the next setup must not take it for a checkout.
    ran = []
    monkeypatch.setattr(embed_setup, "_run", lambda command: ran.append([str(part) for part in command]))
    monkeypatch.setattr(embed_setup.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(embed_setup, "_has_commit", lambda checkout: False)
    (home / "src" / "llama.cpp").mkdir(parents=True)              # what a failed first try left
    with pytest.raises(embed_setup.SetupError):
        embed_setup.install_binary()
    assert any("fetch" in command and command[-1] == embed_setup.LLAMA_COMMIT for command in ran)


def test_the_download_says_where_from_how_large_and_how_far_it_is(home, served, monkeypatch):
    # On a slow link the 310 MB took 1.7 hours here; a line every 32 MB (11 minutes apart) looked like a hang.
    monkeypatch.setattr(embed_setup, "PROGRESS_SECONDS", 0.0)
    said = []
    embed_setup.install_model(None, said.append)
    assert said[0].startswith("downloading the model") and "127.0.0.1" in said[0] and "MB" in said[0]
    assert "setup again" in said[0]                                     # an interruption is not a loss
    progress = [line for line in said[1:] if "%" in line]
    assert progress and "MB/s" in progress[-1] and "100%" in progress[-1]


def test_two_installs_at_once_wait_for_each_other(home):
    # A second terminal (or a retry while the first still downloads) built llama.cpp in the same directory.
    said, inside = [], threading.Event()

    def second():
        with embed_setup.installing(said.append):
            inside.set()

    with embed_setup.installing(lambda text: None):
        other = threading.Thread(target=second)
        other.start()
        assert not inside.wait(0.5)                                     # held while the first is at it
        assert said and "waiting" in said[0]
    other.join(5)
    assert inside.is_set()


def test_a_missing_model_file_is_said_before_anything_is_built(home, monkeypatch, capsys):
    monkeypatch.setattr(embed_setup, "install_binary", lambda *a, **k: pytest.fail("built before the file was looked at"))
    assert cli.main(["embed", "setup", "--model-file", "/no/such/model.gguf"]) == 2
    err = capsys.readouterr().err
    assert "/no/such/model.gguf" in err and "Traceback" not in err


def test_a_connection_that_stays_slow_is_replaced_and_the_download_goes_on_where_it_was(home, monkeypatch):
    # Measured: Hugging Face's CDN gives one connection 40 KB/s and the next 2.9 MB/s for the same bytes. A download
    # that keeps a slow connection takes hours; one that starts a new connection when the last stays slow does not.
    import time as clock
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            start = int(self.headers["Range"].removeprefix("bytes=").rstrip("-")) if self.headers.get("Range") else 0
            asked.append(start)
            body = CONTENT[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            slow = len(asked) <= 2                                       # the first two connections crawl
            for at in range(0, len(body), 4096):
                self.wfile.write(body[at:at + 4096])
                if slow:
                    clock.sleep(0.05)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(embed_setup, "MODEL_URL", f"http://127.0.0.1:{server.server_port}/model.gguf")
    monkeypatch.setattr(embed_setup, "_READ", 4096)
    monkeypatch.setattr(embed_setup, "SLOW_WINDOW_SECONDS", 0.3)
    monkeypatch.setattr(embed_setup, "SLOW_BYTES_PER_SECOND", 200_000)
    try:
        assert embed_setup.install_model().read_bytes() == CONTENT
    finally:
        server.shutdown()
    assert len(asked) == 3 and asked[0] == 0 and 0 < asked[1] < asked[2] < len(CONTENT)     # resumed each time


def test_a_whole_part_left_by_an_earlier_try_is_checked_without_asking_for_more(home, served):
    part = worker.model().with_suffix(".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(CONTENT)
    assert embed_setup.install_model().read_bytes() == CONTENT


def test_a_connection_that_drops_early_is_resumed_and_what_came_is_never_thrown_away(home, monkeypatch):
    # http.client returns an empty read when the peer closes early: that is not the end of the file.
    asked = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            start = int(self.headers["Range"].removeprefix("bytes=").rstrip("-")) if self.headers.get("Range") else 0
            asked.append(start)
            body = CONTENT[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body if start else body[:len(body) // 2])      # the first connection drops halfway
            self.close_connection = True

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(embed_setup, "MODEL_URL", f"http://127.0.0.1:{server.server_port}/model.gguf")
    try:
        assert embed_setup.install_model().read_bytes() == CONTENT
        assert asked == [0, len(CONTENT) // 2]
        worker.model().unlink()
        asked.clear()
        monkeypatch.setattr(embed_setup, "RECONNECTS", 0)                  # no second connection allowed
        with pytest.raises(embed_setup.SetupError, match="again"):
            embed_setup.install_model()
        assert worker.model().with_suffix(".part").stat().st_size == len(CONTENT) // 2      # kept for the next try
    finally:
        server.shutdown()


def test_the_worker_says_what_it_was_built_from_and_is_out_of_date_when_the_pin_moves(home, monkeypatch):
    for path in (worker.binary(), worker.model()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    assert embed_setup.current()                                           # built before records were kept
    embed_setup._record(embed_setup.LLAMA_COMMIT)
    assert embed_setup.current()
    monkeypatch.setattr(embed_setup, "LLAMA_COMMIT", "e" * 40)
    assert not embed_setup.current()
