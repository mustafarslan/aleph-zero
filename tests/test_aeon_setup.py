"""Installing Aeon's core for memory by meaning: built once per machine into Aleph's cache, found there without
ALEPH_AEON_LIB, so the plugin's engine and a person's commands use it alike."""
import shutil
import sys
from pathlib import Path

import pytest

from aleph_zero import cli, memory
from aleph_zero import aeon_setup

AEON_LIB = Path(__file__).resolve().parents[1] / "bench" / "data" / "aeon-build" / "lib"
needs_aeon = pytest.mark.skipif(not list(AEON_LIB.glob("core.cpython-*.so")),
                                reason="needs a build of Aeon's core in bench/data/aeon-build (the M0 probe)")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("ALEPH_AEON_LIB", raising=False)
    memory._loaded.clear()
    yield
    memory._loaded.clear()


def test_the_build_is_portable_release_without_tests_for_the_engine_s_own_python(tmp_path):
    steps = aeon_setup.build_steps(tmp_path / "src", tmp_path / "build")
    configure = " ".join(steps[0])
    assert "-DCMAKE_BUILD_TYPE=Release" in configure and "-DAEON_CI_BUILD=ON" in configure   # not -march=native
    assert "-DBUILD_TESTING=OFF" in configure and f"-DPython_EXECUTABLE={sys.executable}" in configure
    assert steps[1][-2:] == ["--target", "aeon_py_core"]                                    # the extension only


def test_nothing_is_installed_until_it_is_and_the_cache_follows_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert aeon_setup.where() == tmp_path / "cache" / "aleph-zero" / "aeon"
    assert not aeon_setup.installed() and memory._aeon_core() is None
    assert not (tmp_path / "cache" / "aleph-zero" / "aeon").exists()                       # looking creates nothing


@needs_aeon
def test_a_build_in_the_cache_is_found_without_aleph_aeon_lib(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    lib = aeon_setup.lib_dir()
    lib.mkdir(parents=True)
    for built in AEON_LIB.glob("core.cpython-*.so"):
        shutil.copy2(built, lib / built.name)
    assert aeon_setup.installed()
    # In a process of its own: an extension module can be loaded once per process, and other tests load Aeon's.
    import os
    import subprocess
    env = {key: value for key, value in os.environ.items() if key != "ALEPH_AEON_LIB"}
    found = subprocess.run([sys.executable, "-c", "from aleph_zero import memory; core = memory._aeon_core(); "
                            "print(core is not None and hasattr(core, 'TraceManager'))"],
                           env={**env, "XDG_CACHE_HOME": str(tmp_path / "cache")}, capture_output=True, text=True)
    assert found.stdout.strip() == "True", found.stderr


def test_the_compiler_is_checked_first_and_how_to_get_it_is_said(tmp_path, monkeypatch):
    from aleph_zero import buildtools

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(buildtools, "compiler_found", lambda: False)
    with pytest.raises(buildtools.ToolError, match="xcode-select --install" if sys.platform == "darwin" else "compiler"):
        aeon_setup.install(None, lambda text: None)


def test_setup_memory_installs_from_a_local_checkout_from_any_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    built = []
    monkeypatch.setattr(aeon_setup, "install", lambda source=None, report=None: built.append(source) or Path("/aeon/lib"))
    assert cli.main(["--cwd", str(tmp_path), "setup", "--memory", "--aeon-source", str(tmp_path / "aeon")]) == 0
    assert built == [tmp_path / "aeon"] and "installed" in capsys.readouterr().out


def test_a_build_says_what_it_was_built_from_and_is_out_of_date_when_the_pin_moves(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    lib = aeon_setup.lib_dir()
    lib.mkdir(parents=True)
    (lib / ("core" + aeon_setup._suffix())).write_bytes(b"")
    aeon_setup._record(aeon_setup.AEON_COMMIT)
    assert aeon_setup.installed() and aeon_setup.current()
    monkeypatch.setattr(aeon_setup, "AEON_COMMIT", "f" * 40)                 # a newer Aleph pins a newer Aeon
    assert aeon_setup.installed() and not aeon_setup.current()
    aeon_setup._record("local:/somewhere/aeon")                            # built from a checkout the person chose
    assert aeon_setup.current()


def test_the_setup_list_says_when_aeon_is_out_of_date(repo, capsys, monkeypatch):
    from aleph_zero import firstrun, memory

    monkeypatch.setattr(memory, "aeon_available", lambda: True)
    monkeypatch.setattr(aeon_setup, "current", lambda: False)
    monkeypatch.setattr(aeon_setup, "installed", lambda: True)
    by = {step.key: step for step in firstrun.of(__import__("aleph_zero.engine", fromlist=["Engine"]).Engine(repo.root).status())}
    assert by["memory"].state == "open" and "newer" in by["memory"].detail
