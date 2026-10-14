"""Installing the embedding worker: the llama.cpp server and the model file.

This is the only part of Aleph that uses the network, and only when asked to
(`aleph embed setup`). The server is built from a pinned llama.cpp commit with one
patch (an embeddings-only context must not reserve a logits buffer: 1 MB per token
with this model's vocabulary, ADR-005). Building needs git, cmake, ninja and a C++
compiler, which is acceptable on the owner's machine and is recorded as a blocker
for a public release.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import worker

LLAMA_REPOSITORY = "https://github.com/ggml-org/llama.cpp"
LLAMA_COMMIT = "c83f3058b6f876f2f7e6ccbada858b2446860c85"
MODEL_URL = f"https://huggingface.co/unsloth/embeddinggemma-2-GGUF/resolve/main/{worker.MODEL_FILE}"
MODEL_SHA256 = "6f1bd4ac6c5df7444f9cca7ca36cafe6cfa34cd6f49fefb1e0b4be8143aed8bc"
PATCH = Path(__file__).with_name("llama-no-logits.patch")
_CHUNK = 1 << 20


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
    have = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(MODEL_URL, headers={"Range": f"bytes={have}-"} if have else {})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            resumed = response.status == 206
            with open(part, "ab" if resumed else "wb") as out:
                done = have if resumed else 0
                while block := response.read(_CHUNK):
                    out.write(block)
                    done += len(block)
                    if done % (32 * _CHUNK) < _CHUNK:
                        report(f"model: {done / 1e6:.0f} MB")
    except (urllib.error.URLError, OSError) as exc:
        raise SetupError(f"the model could not be downloaded from {MODEL_URL}: {exc}; run setup again to resume") from exc


def build_steps(source: Path, build: Path, metal: bool) -> list[list[str]]:
    """The commands that produce a patched, statically linked llama-server."""
    return [
        ["git", "-C", str(source), "checkout", "--quiet", "--force", LLAMA_COMMIT],
        ["git", "-C", str(source), "apply", str(PATCH)],
        ["cmake", "-S", str(source), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
         "-DBUILD_SHARED_LIBS=OFF", "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF", "-DLLAMA_CURL=OFF",
         f"-DGGML_METAL={'ON' if metal else 'OFF'}"],
        ["cmake", "--build", str(build), "--target", "llama-server"],
    ]


def install_binary(source: Path | None = None, report=lambda text: None) -> Path:
    """Build llama-server and put it in the worker's home. `source` is an existing
    llama.cpp checkout to build from (no network); without it the repository is cloned."""
    home = worker.home()
    for tool in ("git", "cmake", "ninja"):
        if shutil.which(tool) is None:
            raise SetupError(f"{tool} is needed to build the worker and was not found")
    checkout = home / "src" / "llama.cpp"
    if not checkout.exists():
        checkout.parent.mkdir(parents=True, exist_ok=True)
        if source is not None:
            report(f"copying {source}")
            _run(["git", "clone", "--quiet", "--no-hardlinks", str(source), str(checkout)])
        else:
            # The one commit that is built, not the project's history (hundreds of megabytes).
            report(f"fetching {LLAMA_REPOSITORY} at {LLAMA_COMMIT[:12]}")
            _run(["git", "init", "--quiet", str(checkout)])
            _run(["git", "-C", str(checkout), "fetch", "--quiet", "--depth", "1", LLAMA_REPOSITORY, LLAMA_COMMIT])
    build = home / "build"
    for step in build_steps(checkout, build, metal=sys.platform == "darwin"):
        report(" ".join(step[:4]) + " …")
        _run(step)
    built = build / "bin" / "llama-server"
    if not built.exists():
        raise SetupError(f"the build finished without producing {built}")
    target = worker.binary()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, target)
    return target


def _run(command: list[str]) -> None:
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise SetupError(f"{' '.join(command[:3])} failed: {(done.stderr or done.stdout).strip()[-400:]}")


def installed() -> bool:
    """Whether the worker and its model are there. Only looks: asking does not create the worker's directory."""
    root = worker.where()
    return (root / "bin" / "llama-server").exists() and (root / "models" / worker.MODEL_FILE).exists()
