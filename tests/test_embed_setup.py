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


def test_the_build_is_static_patched_and_pinned(home):
    steps = embed_setup.build_steps(Path("/src/llama.cpp"), Path("/build"), metal=True)
    joined = [" ".join(step) for step in steps]
    assert any(f"checkout --quiet --force {embed_setup.LLAMA_COMMIT}" in step for step in joined)
    assert any("apply" in step and "llama-no-logits.patch" in step for step in joined)
    configure = next(step for step in joined if step.startswith("cmake -S"))
    assert "-DBUILD_SHARED_LIBS=OFF" in configure and "-DGGML_METAL=ON" in configure and "-DLLAMA_CURL=OFF" in configure
    assert any("--target llama-server" in step for step in joined)
    assert "-DGGML_METAL=OFF" in " ".join(" ".join(s) for s in embed_setup.build_steps(Path("/s"), Path("/b"), metal=False))
    assert (Path(embed_setup.__file__).parent / "llama-no-logits.patch").exists()


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
