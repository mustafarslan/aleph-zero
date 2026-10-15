"""Installing Aeon's core, for recalling memories by meaning (ADR-006).

Aeon is a C++ engine with a Python extension and is only built from source, so this builds that one extension,
from one pinned commit, for the Python this engine runs under, into Aleph's cache directory, where `memory`
finds it with no setting (`ALEPH_AEON_LIB` still wins when set). Only when asked (`aleph setup --memory`); it
needs git, cmake, ninja, a C++23 compiler and SIMDe (`brew install simde`), and the network for the source unless
a checkout is given (`--aeon-source`)."""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

AEON_REPOSITORY = "https://github.com/mustafarslan/aeon"
AEON_COMMIT = ""   # the commit of the branch Aleph's fixes are on; set when it is published


class SetupError(RuntimeError):
    pass


def where() -> Path:
    """Where the build is or would be. Nothing is created: for those who only look."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "aleph-zero" / "aeon"


def lib_dir() -> Path:
    return where() / "lib"


def _suffix() -> str:
    return sysconfig.get_config_var("EXT_SUFFIX") or ".so"     # e.g. .cpython-313-darwin.so


def installed() -> bool:
    """A build for this Python is there. Only looks."""
    return (lib_dir() / ("core" + _suffix())).exists()


def build_steps(source: Path, build: Path) -> list[list[str]]:
    """Configure and build Aeon's Python extension alone: portable flags (not -march=native: the build may be
    copied between Macs), no tests or benchmarks, for this engine's Python."""
    return [
        ["cmake", "-S", str(source), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
         "-DAEON_CI_BUILD=ON", "-DBUILD_TESTING=OFF", f"-DPython_EXECUTABLE={sys.executable}"],
        ["cmake", "--build", str(build), "--target", "aeon_py_core"],
    ]


def _simde_found() -> bool:
    for prefix in (os.environ.get("HOMEBREW_PREFIX"), "/opt/homebrew", "/usr/local", "/usr"):
        if prefix and (Path(prefix) / "include" / "simde" / "x86" / "avx512.h").exists():
            return True
    return False


@contextmanager
def _installing(report) -> Iterator[None]:
    """One build at a time: a second terminal waits for the first."""
    home = where()
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "install.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report("another `aleph setup --memory` is building Aeon on this machine; waiting for it to finish")
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def install(source: Path | None = None, report=lambda text: None) -> Path:
    """Build Aeon's core into the cache and return the directory it is in. `source` is an Aeon checkout to build
    from (no network); without it the pinned commit is fetched."""
    for tool in ("git", "cmake", "ninja"):
        if shutil.which(tool) is None:
            raise SetupError(f"{tool} is needed to build Aeon and was not found")
    if not _simde_found():
        raise SetupError("SIMDe is needed to build Aeon and was not found: brew install simde")
    if source is None and not AEON_COMMIT:
        raise SetupError("this build of Aleph names no Aeon commit to fetch; give a checkout with --aeon-source")
    if source is not None and not (Path(source) / "core" / "CMakeLists.txt").exists():
        raise SetupError(f"{source} is not an Aeon checkout (no core/CMakeLists.txt)")
    with _installing(report):
        home = where()
        checkout = Path(source) if source is not None else home / "src"
        if source is None and not _has_commit(checkout):
            report(f"fetching {AEON_REPOSITORY} at {AEON_COMMIT[:12]}")
            checkout.mkdir(parents=True, exist_ok=True)
            _run(["git", "init", "--quiet", str(checkout)])
            _run(["git", "-C", str(checkout), "fetch", "--quiet", "--depth", "1", AEON_REPOSITORY, AEON_COMMIT])
            _run(["git", "-C", str(checkout), "checkout", "--quiet", "--force", AEON_COMMIT])
        build = home / "build"
        for step in build_steps(checkout, build):
            report(" ".join(step[:4]) + " …")
            _run(step)
        built = sorted((build / "lib").glob("core" + _suffix()))
        if not built:
            raise SetupError(f"the build finished without producing {build / 'lib' / ('core' + _suffix())}")
        lib = lib_dir()
        lib.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built[0], lib / built[0].name)
        return lib


def _has_commit(checkout: Path) -> bool:
    if not (checkout / ".git").exists():
        return False
    found = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--verify", "--quiet", "HEAD"], capture_output=True, text=True)
    return found.returncode == 0 and found.stdout.strip() == AEON_COMMIT


def _run(command: list[str]) -> None:
    done = subprocess.run(command, capture_output=True, text=True)
    if done.returncode != 0:
        raise SetupError(f"{' '.join(command[:3])} failed: {(done.stderr or done.stdout).strip()[-400:]}")
