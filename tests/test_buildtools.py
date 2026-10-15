"""The tools the two builds need (the embedding worker, Aeon's core), fetched when they are missing: cmake and ninja
from PyPI with uv, SIMDe at one pinned release. Only the C++ compiler is the person's to install."""
import sys
from pathlib import Path

import pytest

from aleph_zero import buildtools


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(buildtools, "compiler_found", lambda: True)
    return tmp_path / "cache" / "aleph-zero" / "tools"


def test_without_a_compiler_the_person_is_told_the_one_command_that_installs_it(cache, monkeypatch):
    monkeypatch.setattr(buildtools, "compiler_found", lambda: False)
    with pytest.raises(buildtools.ToolError, match="xcode-select --install" if sys.platform == "darwin" else "C\\+\\+ compiler"):
        buildtools.ensure(lambda text: None)


def test_cmake_and_ninja_on_path_are_used_and_nothing_is_fetched(cache, monkeypatch):
    monkeypatch.setattr(buildtools.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(buildtools, "_run", lambda command: pytest.fail(f"ran {command}"))
    tools = buildtools.ensure(lambda text: None)
    assert (tools.cmake, tools.ninja) == ("/usr/bin/cmake", "/usr/bin/ninja")


def test_missing_cmake_and_ninja_are_fetched_once_into_the_cache_with_uv(cache, monkeypatch):
    monkeypatch.setattr(buildtools.shutil, "which", lambda name: "/opt/uv/bin/uv" if name == "uv" else
                        "/usr/bin/git" if name == "git" else None)
    ran, said = [], []

    def run(command):
        ran.append(command)
        if command[1] == "pip":
            for tool in ("cmake", "ninja"):
                path = cache / "venv" / "bin" / tool
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("#!/bin/sh\n")
                path.chmod(0o755)

    monkeypatch.setattr(buildtools, "_run", run)
    tools = buildtools.ensure(said.append)
    assert tools.cmake == str(cache / "venv" / "bin" / "cmake") and tools.ninja == str(cache / "venv" / "bin" / "ninja")
    assert [command[:2] for command in ran] == [["/opt/uv/bin/uv", "venv"], ["/opt/uv/bin/uv", "pip"]]
    assert "cmake" in ran[1] and "ninja" in ran[1] and any("fetching cmake and ninja" in line for line in said)
    ran.clear()
    buildtools.ensure(said.append)
    assert ran == []                                                        # once


def test_without_uv_the_missing_tools_are_named(cache, monkeypatch):
    monkeypatch.setattr(buildtools.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    monkeypatch.setattr(buildtools, "_uv", lambda: None)
    with pytest.raises(buildtools.ToolError, match="cmake and ninja"):
        buildtools.ensure(lambda text: None)


def test_simde_is_fetched_at_its_pinned_release_once(cache, monkeypatch):
    ran = []

    def run(command):
        ran.append(command)
        header = Path(command[-1]) / "simde" / "x86" / "avx512.h"
        header.parent.mkdir(parents=True, exist_ok=True)
        header.write_text("")

    monkeypatch.setattr(buildtools, "_run", run)
    found = buildtools.simde(lambda text: None)
    assert (found / "simde" / "x86" / "avx512.h").exists()
    assert ran[0][:2] == ["git", "clone"] and buildtools.SIMDE_TAG in ran[0] and "--depth" in ran[0]
    assert buildtools.simde(lambda text: None) == found and len(ran) == 1


def test_the_builds_name_their_tools_and_aeon_its_simde(tmp_path):
    from aleph_zero import aeon_setup
    from aleph_zero.embed import setup as embed_setup

    tools = buildtools.Tools(cmake="/c/cmake", ninja="/c/ninja")
    aeon = aeon_setup.build_steps(tmp_path / "src", tmp_path / "build", tools, tmp_path / "simde")
    assert aeon[0][0] == "/c/cmake" and "-DCMAKE_MAKE_PROGRAM=/c/ninja" in aeon[0]
    assert f"-DSIMDE_INCLUDE_DIR={tmp_path / 'simde'}" in aeon[0] and aeon[1][0] == "/c/cmake"
    worker = embed_setup.build_steps(Path("/s"), Path("/b"), metal=True, tools=tools)
    configure = next(step for step in worker if "-S" in step)
    assert configure[0] == "/c/cmake" and "-DCMAKE_MAKE_PROGRAM=/c/ninja" in configure
    assert "-DGGML_CCACHE=OFF" in configure          # CMake finds Homebrew's ccache off PATH; the build then cannot run it
