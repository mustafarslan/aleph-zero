import subprocess
from pathlib import Path

import pytest


class Repo:
    """A throwaway git repository for tests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout

    def write(self, path: str, content: str) -> Path:
        full = self.root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        return full

    def commit(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


def _init(root: Path) -> Repo:
    root.mkdir(parents=True, exist_ok=True)
    repo = Repo(root)
    repo.git("init", "-q", "-b", "main")
    repo.git("config", "user.email", "test@example.com")
    repo.git("config", "user.name", "Test")
    repo.git("config", "commit.gpgsign", "false")
    return repo


@pytest.fixture
def make_repo():
    """A function that creates another throwaway repository at a given path."""
    return _init


@pytest.fixture
def empty_repo(tmp_path) -> Repo:
    return _init(tmp_path / "repo")


@pytest.fixture
def repo(empty_repo) -> Repo:
    empty_repo.write("src/auth/token_store.py", (
        "MAX_RETRY_BACKOFF_EXPONENT = 6\n\n\n"
        "def refresh_token(session):\n"
        "    \"\"\"Exchange a refresh token for a new access token.\"\"\"\n"
        "    return session.post('/oauth/refresh')\n\n\n"
        "def revokeAllSessions(user):\n"
        "    for session in user.sessions:\n"
        "        session.revoke()\n"
    ))
    empty_repo.write("src/billing/invoice.py", (
        "def render_invoice(order):\n"
        "    total = sum(line.amount for line in order.lines)\n"
        "    return f'Invoice total: {total}'\n"
    ))
    empty_repo.write("docs/guide.md", (
        "# Guide\n\nHow the service works.\n\n"
        "## Authentication\n\nTokens expire after an hour and are refreshed silently.\n\n"
        "## Billing\n\nInvoices are rendered nightly.\n"
    ))
    empty_repo.commit("initial")
    return empty_repo


@pytest.fixture
def fake_worker():
    """A stand-in for the embedding worker, listening on a UNIX socket. Yields the socket's path."""
    import shutil
    import sys
    import tempfile
    import time

    directory = Path(tempfile.mkdtemp(prefix="az-", dir="/tmp"))  # short: a socket path is limited to 104 bytes
    sock = directory / "w.sock"
    proc = subprocess.Popen([sys.executable, str(Path(__file__).parent / "fake_worker.py"), "--host", str(sock), "-c", "4096"])
    from aleph_zero.embed.client import EmbedClient

    deadline = time.time() + 10
    while time.time() < deadline and not (sock.exists() and EmbedClient(sock).healthy()):
        time.sleep(0.02)  # the socket file appears before the server accepts connections
    yield sock
    proc.terminate()
    proc.wait(timeout=10)
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_real_worker(monkeypatch, tmp_path_factory):
    """Tests never see the embedding worker installed on this machine, nor its cache."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("cache")))
    monkeypatch.delenv("ALEPH_DENSE", raising=False)


def pytest_sessionfinish(session, exitstatus):
    """No test may make the grammar package fetch its grammars again. It keeps them under HOME, so a test
    that points HOME at an empty directory and then parses anything (in this process or in a server it
    starts) downloads them all over the network, silently. Found once (M10); this keeps it found."""
    factory = getattr(session.config, "_tmp_path_factory", None)
    if factory is None:
        return
    base = factory.getbasetemp()
    fetched = [path for pattern in ("*/Library/Caches/tree-sitter-language-pack", "*/*/Library/Caches/tree-sitter-language-pack",
                                    "*/*/*/Library/Caches/tree-sitter-language-pack", "*/.cache/tree-sitter-language-pack",
                                    "*/*/.cache/tree-sitter-language-pack", "*/*/*/.cache/tree-sitter-language-pack")
               for path in base.glob(pattern)]
    if fetched:
        session.exitstatus = 1
        print(f"\nFAILED: grammars were fetched into a temporary home during the tests ({fetched[0]}): a test changed HOME "
              f"in the environment and then parsed something. Change `Path.home` for the process instead.")
