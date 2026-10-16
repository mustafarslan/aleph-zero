"""Installing the embedding worker: the llama.cpp server and the model file.

This is the only part of Aleph that uses the network, and only when asked to
(`aleph embed setup`). The server is built from a pinned llama.cpp commit as it is: the patch an
embeddings-only context once needed (no logits buffer: 1 MB per token with this model's vocabulary,
ADR-005) is upstream since PR #30255, and moving to it left every vector as it was (DEFERRED 4). Building needs git, cmake, ninja and a C++
compiler, which is acceptable on the owner's machine and is recorded as a blocker
for a public release.
"""
from __future__ import annotations

import fcntl
import hashlib
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import worker
from ..names import COMMAND

LLAMA_REPOSITORY = "https://github.com/ggml-org/llama.cpp"
LLAMA_COMMIT = "b76b13379d3b7d67ab0bc424516bb6fd2522bd8e"
MODEL_URL = f"https://huggingface.co/unsloth/embeddinggemma-2-GGUF/resolve/main/{worker.MODEL_FILE}"
MODEL_SHA256 = "6f1bd4ac6c5df7444f9cca7ca36cafe6cfa34cd6f49fefb1e0b4be8143aed8bc"
_CHUNK = 1 << 20
_READ = 1 << 16          # what one read of the download waits for: a slow link still shows progress
PROGRESS_SECONDS = 30.0  # between progress lines when they are not redrawn in place on a terminal
SLOW_WINDOW_SECONDS = 10.0          # how long a connection is watched before it is judged slow
SLOW_BYTES_PER_SECOND = 256 * 1024  # under this for a whole window, it is replaced by a new connection
RECONNECTS = 40                     # new connections at most for one download


class SetupError(RuntimeError):
    """The worker could not be installed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while block := handle.read(_CHUNK):
            digest.update(block)
    return digest.hexdigest()


def install_model(local: Path | None = None, report=lambda text: None) -> Path:
    """Put the model file in the worker's home: from `local` if given, else by download.
    A partial download is resumed. The file is kept only if its checksum is the pinned one."""
    target = worker.model()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and _sha256(target) == MODEL_SHA256:
        return target
    part = target.with_suffix(".part")
    if local is not None:
        shutil.copyfile(local, part)
    else:
        _download(part, report)
    found = _sha256(part)
    if found != MODEL_SHA256:
        part.unlink(missing_ok=True)
        raise SetupError(f"the model file's checksum is {found[:16]}…, expected {MODEL_SHA256[:16]}…; nothing was installed")
    part.replace(target)
    return target


def _download(part: Path, report) -> None:
    """Fetch the model into `part`, continuing what an earlier try left. Says where from and how large first, then how
    far it is: redrawn in place on a terminal, a line every PROGRESS_SECONDS otherwise. A connection that stays slow is
    replaced by a new one that continues where it was: the CDN's speed differs from one connection to the next (40 KB/s
    and 2.9 MB/s for the same bytes, measured), and keeping a slow one made the 310 MB take hours."""
    live = sys.stderr.isatty()
    total = started = None
    shown, got = 0.0, 0
    try:
        for attempt in range(RECONNECTS + 1):
            have = part.stat().st_size if part.exists() else 0
            if total is not None and have >= total:
                break
            request = urllib.request.Request(MODEL_URL, headers={"Range": f"bytes={have}-"} if have else {})
            try:
                response = urllib.request.urlopen(request, timeout=60)
            except urllib.error.HTTPError as exc:
                if exc.code == 416 and have:    # nothing after what is there: an earlier try got it all
                    break
                raise
            with response:
                resumed = response.status == 206
                done = have if resumed else 0
                if total is None:
                    total = done + int(response.headers.get("Content-Length") or 0)
                    host = urllib.parse.urlsplit(MODEL_URL).hostname
                    report(f"downloading the model ({total / 1e6:.0f} MB) from {host}"
                           + (f", from {done / 1e6:.0f} MB where the last try stopped" if done else "")
                           + "; if it is interrupted, setup again continues where it stopped")
                    started = time.monotonic()
                window_at, window = time.monotonic(), 0
                slow = False
                with open(part, "ab" if resumed else "wb") as out:
                    while block := response.read(_READ):
                        out.write(block)
                        done += len(block)
                        got += len(block)
                        window += len(block)
                        now = time.monotonic()
                        if now - shown >= (1.0 if live else PROGRESS_SECONDS):
                            shown = now
                            _progress(report, live, done, total, got / max(now - started, 1e-6))
                        if now - window_at >= SLOW_WINDOW_SECONDS:
                            if window / (now - window_at) < SLOW_BYTES_PER_SECOND and attempt < RECONNECTS:
                                slow = True     # this connection crawls: a new one continues from here
                                break
                            window_at, window = now, 0
            if not slow and (total is None or done >= total):
                break               # the whole file; a connection that ended early is continued like a slow one
        else:
            have = part.stat().st_size if part.exists() else 0
            if total is not None and have < total:
                raise SetupError(f"the download stopped at {have / 1e6:.0f} of {total / 1e6:.0f} MB; run setup again "
                                 f"to continue from there")
        if total is not None:
            _progress(report, live, part.stat().st_size, total, got / max(time.monotonic() - started, 1e-6), last=True)
    except (urllib.error.URLError, OSError) as exc:
        raise SetupError(f"the model could not be downloaded from {MODEL_URL}: {exc}; run setup again to resume") from exc


def build_steps(source: Path, build: Path, metal: bool, tools=None) -> list[list[str]]:
    """The commands that produce a statically linked llama-server, with the cmake and ninja buildtools found or
    fetched."""
    cmake = tools.cmake if tools else "cmake"
    return [
        ["git", "-C", str(source), "checkout", "--quiet", "--force", LLAMA_COMMIT],
        [cmake, "-S", str(source), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
         "-DBUILD_SHARED_LIBS=OFF", "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_CURL=OFF",
         # No ccache: useless for a one-time build, and CMake finds Homebrew's even off PATH, after which the build,
         # run with a short PATH (Claude Code started from the Dock), cannot (seen 2026-10-15).
         "-DGGML_CCACHE=OFF",
         f"-DGGML_METAL={'ON' if metal else 'OFF'}"] + ([f"-DCMAKE_MAKE_PROGRAM={tools.ninja}"] if tools else []),
        [cmake, "--build", str(build), "--target", "llama-server"],
    ]


def _has_commit(checkout: Path) -> bool:
    """Whether a checkout holds the pinned commit (a fetch that was interrupted leaves the directory without it)."""
    if not (checkout / ".git").exists():
        return False
    found = subprocess.run(["git", "-C", str(checkout), "cat-file", "-e", f"{LLAMA_COMMIT}^{{commit}}"],
                           capture_output=True)
    return found.returncode == 0


def _progress(report, live: bool, done: int, total: int, rate: float, last: bool = False) -> None:
    left = (total - done) / rate / 60 if rate > 0 and total > done else 0
    text = (f"model: {done / 1e6:.0f} of {total / 1e6:.0f} MB ({100 * done / max(total, 1):.0f}%), {rate / 1e6:.2f} MB/s"
            + (f", about {left:.0f} min left" if left >= 1 else ""))
    if live:
        sys.stderr.write(f"\r{text}   " + ("\n" if last else ""))
        sys.stderr.flush()
    else:
        report(text)


@contextmanager
def installing(report=lambda text: None) -> Iterator[None]:
    """One installation of the worker at a time: a second terminal, or a retry while the first still downloads, waits
    for the first instead of building llama.cpp in the same directory."""
    with open(worker.home() / "install.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report(f"another `{COMMAND} setup` is installing the worker on this machine; waiting for it to finish")
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def install_binary(source: Path | None = None, report=lambda text: None) -> Path:
    """Build llama-server and put it in the worker's home. `source` is an existing
    llama.cpp checkout to build from (no network); without it the repository is cloned."""
    home = worker.home()
    from .. import buildtools

    tools = buildtools.ensure(report)        # the compiler checked; cmake and ninja from PATH or fetched once
    checkout = home / "src" / "llama.cpp"
    if source is not None and not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        report(f"copying {source}")
        _run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(checkout)])
    elif source is None and not _has_commit(checkout):
        # The one commit that is built, not the project's history (hundreds of megabytes). Asked again when an
        # earlier try was interrupted after `git init`: the directory is there, the commit is not.
        checkout.parent.mkdir(parents=True, exist_ok=True)
        report(f"fetching {LLAMA_REPOSITORY} at {LLAMA_COMMIT[:12]}")
        _run(["git", "init", "--quiet", str(checkout)])
        _run(["git", "-C", str(checkout), "fetch", "--quiet", "--depth", "1", LLAMA_REPOSITORY, LLAMA_COMMIT])
    build = home / "build"
    for step in build_steps(checkout, build, metal=sys.platform == "darwin", tools=tools):
        report(" ".join(step[:4]) + " …")
        _run(step)
    built = build / "bin" / "llama-server"
    if not built.exists():
        raise SetupError(f"the build finished without producing {built}")
    target = worker.binary()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, target)
    _record(f"local:{Path(source).resolve()}" if source is not None else LLAMA_COMMIT)
    return target


def _run(command: list[str]) -> None:
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise SetupError(f"{' '.join(command[:3])} failed: {(done.stderr or done.stdout).strip()[-400:]}")


def _record(built_from: str) -> None:
    (worker.home() / "built-from").write_text(built_from + "\n")


def built_from() -> str | None:
    try:
        return (worker.where() / "built-from").read_text().strip() or None
    except OSError:
        return None


def current() -> bool:
    """The worker is built from the llama.cpp commit this Aleph pins (or from a checkout the person gave). A build
    from an older pin keeps working; `setup --worker` or `update` builds the new one."""
    found = built_from()
    return installed() and (found is None or found == LLAMA_COMMIT or found.startswith("local:"))


def installed() -> bool:
    """Whether the worker and its model are there. Only looks: asking does not create the worker's directory."""
    root = worker.where()
    return (root / "bin" / "llama-server").exists() and (root / "models" / worker.MODEL_FILE).exists()
