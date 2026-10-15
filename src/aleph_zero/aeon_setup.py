"""Installing Aeon's core, for recalling memories by meaning (ADR-006).

Aeon is a C++ engine with a Python extension and is only built from source, so this builds that one extension,
from one pinned commit, for the Python this engine runs under, into Aleph's cache directory, where `memory`
finds it with no setting (`ALEPH_AEON_LIB` still wins when set). Only when asked (`aleph setup --memory`); it
needs a C++23 compiler (Xcode's command-line tools on macOS); git, cmake, ninja and SIMDe are found or fetched by
buildtools; the network is used for the source unless a checkout is given (`--aeon-source`)."""
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
AEON_COMMIT = "c7128c959a3342d63ed1f9bd8e789305f6b4b567"   # Aeon branch aleph-memory-fixes (off v4), with the fixes memory needs


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


def _record(built_from: str) -> None:
    lib_dir().mkdir(parents=True, exist_ok=True)
    (lib_dir() / "built-from").write_text(built_from + "\n")


def built_from() -> str | None:
    try:
        return (lib_dir() / "built-from").read_text().strip() or None
    except OSError:
        return None


def current() -> bool:
    """The build is of the Aeon this Aleph pins (or of a checkout the person chose). False for a build from an
    older pin: a newer Aleph brings a newer Aeon only once it is built again (`setup --memory`, or `update`)."""
    found = built_from()
    # No record: built by 0.1.5, which pinned this same commit and wrote none.
    return installed() and (found is None or found == AEON_COMMIT or found.startswith("local:"))


def build_steps(source: Path, build: Path, tools=None, simde: Path | None = None) -> list[list[str]]:
    """Configure and build Aeon's Python extension alone: portable flags (not -march=native: the build may be
    copied between Macs), no tests or benchmarks, for this engine's Python, with the tools and SIMDe headers
    buildtools found or fetched."""
    cmake = tools.cmake if tools else "cmake"
    return [
        [cmake, "-S", str(source), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
         "-DAEON_CI_BUILD=ON", "-DBUILD_TESTING=OFF", f"-DPython_EXECUTABLE={sys.executable}"]
        + ([f"-DCMAKE_MAKE_PROGRAM={tools.ninja}"] if tools else [])
        + ([f"-DSIMDE_INCLUDE_DIR={simde}"] if simde else []),
        [cmake, "--build", str(build), "--target", "aeon_py_core"],
    ]


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
    if source is None and not AEON_COMMIT:
        raise SetupError("this build of Aleph names no Aeon commit to fetch; give a checkout with --aeon-source")
    if source is not None and not (Path(source) / "core" / "CMakeLists.txt").exists():
        raise SetupError(f"{source} is not an Aeon checkout (no core/CMakeLists.txt)")
    from . import buildtools

    tools = buildtools.ensure(report)        # the compiler checked; cmake and ninja from PATH or fetched once
    headers = buildtools.simde(report)       # SIMDe at its pinned release, fetched once
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
        for step in build_steps(checkout, build, tools, headers):
            report(" ".join(step[:4]) + " …")
            _run(step)
        built = sorted((build / "lib").glob("core" + _suffix()))
        if not built:
            raise SetupError(f"the build finished without producing {build / 'lib' / ('core' + _suffix())}")
        lib = lib_dir()
        lib.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built[0], lib / built[0].name)
        _record(f"local:{Path(source).resolve()}" if source is not None else AEON_COMMIT)
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
