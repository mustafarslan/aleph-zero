"""The tools the two builds need, the embedding worker's (llama.cpp) and Aeon's core: fetched when missing, so that a
person installs nothing by hand but the C++ compiler.

- cmake and ninja: the ones on PATH if there are, else installed once from PyPI into a small environment in the cache
  directory, with uv (which installed Aleph).
- SIMDe (Aeon's portable SIMD headers): one pinned release, fetched once into the cache directory; never the system's,
  so every machine builds the same.
- The C++ compiler: Xcode's command-line tools on macOS, the system's compiler elsewhere. Installing it is the
  person's: on macOS `xcode-select --install` opens Apple's installer, which a program cannot finish for them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SIMDE_REPOSITORY = "https://github.com/simd-everywhere/simde"
SIMDE_TAG = "v0.8.2"

COMPILER_HELP = ("Xcode's command-line tools are needed for their C++ compiler: run `xcode-select --install` (Apple's "
                 "installer opens; a few minutes), then this again" if sys.platform == "darwin" else
                 "a C++ compiler is needed (g++ or clang++, from your system's packages), then this again")


class ToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class Tools:
    cmake: str
    ninja: str


def where() -> Path:
    """Where fetched tools are or would be. Nothing is created: for those who only look."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "aleph-zero" / "tools"


def compiler_found() -> bool:
    if sys.platform == "darwin":
        # `xcode-select -p` fails without the tools; xcrun then finds the compiler inside them.
        for command in (["xcode-select", "-p"], ["xcrun", "--find", "clang++"]):
            try:
                if subprocess.run(command, capture_output=True, timeout=20).returncode != 0:
                    return False
            except (OSError, subprocess.TimeoutExpired):
                return False
        return True
    return any(shutil.which(name) for name in ("c++", "g++", "clang++"))


def _uv() -> str | None:
    """uv: on PATH, or where its installer puts it (Claude Code started from the Dock has a short PATH)."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path(os.environ.get("XDG_BIN_HOME") or Path.home() / ".local" / "bin") / "uv",
                      Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
        if candidate.exists():
            return str(candidate)
    return None


def ensure(report=lambda text: None) -> Tools:
    """The compiler checked, git found, and cmake and ninja to build with: from PATH, or fetched once."""
    if not compiler_found():
        raise ToolError(COMPILER_HELP)
    if shutil.which("git") is None:
        raise ToolError("git is needed to fetch the sources" + (": run `xcode-select --install`" if sys.platform == "darwin" else ""))
    cmake, ninja = shutil.which("cmake"), shutil.which("ninja")
    if cmake and ninja:
        return Tools(cmake, ninja)
    venv = where() / "venv"
    fetched = (venv / "bin" / "cmake", venv / "bin" / "ninja")
    if not all(path.exists() for path in fetched):
        uv = _uv()
        if uv is None:
            raise ToolError("cmake and ninja are needed to build, and uv to fetch them was not found: install uv "
                            "(https://docs.astral.sh/uv/), or cmake and ninja")
        report("fetching cmake and ninja (once, from PyPI, into Aleph's cache)")
        where().mkdir(parents=True, exist_ok=True)
        _run([uv, "venv", "--quiet", str(venv)])
        _run([uv, "pip", "install", "--quiet", "--python", str(venv / "bin" / "python"), "cmake", "ninja"])
        if not all(path.exists() for path in fetched):
            raise ToolError(f"cmake and ninja were installed into {venv} but are not there")
    return Tools(cmake or str(fetched[0]), ninja or str(fetched[1]))


def simde(report=lambda text: None) -> Path:
    """SIMDe's headers at the pinned release, in the cache directory: the include directory Aeon is built with."""
    target = where() / f"simde-{SIMDE_TAG}"
    if (target / "simde" / "x86" / "avx512.h").exists():
        return target
    report(f"fetching SIMDe {SIMDE_TAG} (once)")
    shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--quiet", "--depth", "1", "--branch", SIMDE_TAG, SIMDE_REPOSITORY, str(target)])
    if not (target / "simde" / "x86" / "avx512.h").exists():
        raise ToolError(f"SIMDe {SIMDE_TAG} was fetched into {target} but its headers are not there")
    return target


def _run(command: list[str]) -> None:
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise ToolError(f"{' '.join(Path(part).name if i == 0 else part for i, part in enumerate(command[:3]))} failed: "
                        f"{(done.stderr or done.stdout).strip()[-400:]}")
