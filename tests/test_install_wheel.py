"""Installed as a person installs it: the package built into a wheel and put in a tool directory of its own with
`uv tool install`, then used from there. Catches what a checkout hides: a module or data file left out of the wheel,
an entry point that does not start. Offline, from uv's cache; skipped where uv or its cache is not there."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    where = tmp_path_factory.mktemp("wheel")
    built = subprocess.run([uv, "build", "--quiet", "--wheel", "-o", str(where / "dist"), str(ROOT)], capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    wheel = next((where / "dist").glob("aleph_zero-*.whl"))
    env = {**_env(), "UV_TOOL_DIR": str(where / "tools"), "UV_TOOL_BIN_DIR": str(where / "bin")}
    done = subprocess.run([uv, "tool", "install", "--quiet", "--offline", str(wheel)], capture_output=True, text=True, env=env)
    if done.returncode != 0:
        pytest.skip(f"the dependencies are not in uv's cache, and this test does not use the network: {done.stderr.strip()[-200:]}")
    return where / "bin"


def _env() -> dict:
    import os

    return {key: value for key, value in os.environ.items() if not key.startswith(("VIRTUAL_ENV", "UV_PROJECT"))}


def test_the_installed_command_starts_under_both_names_and_says_the_version(installed):
    version = next(line.split('"')[1] for line in (ROOT / "pyproject.toml").read_text().splitlines() if line.startswith("version"))
    for name in ("aleph-zero", "aleph"):
        done = subprocess.run([str(installed / name), "--version"], capture_output=True, text=True, env=_env())
        assert done.returncode == 0 and done.stdout.strip() == f"aleph-zero {version}", done.stderr


def test_the_installed_engine_indexes_and_searches_a_repository(installed, repo):
    engine = str(installed / "aleph-zero")
    found = subprocess.run([engine, "--cwd", str(repo.root), "search", "refresh token", "--json"], capture_output=True, text=True, env=_env())
    assert found.returncode == 0, found.stderr
    status = json.loads(subprocess.run([engine, "--cwd", str(repo.root), "status"], capture_output=True, text=True, env=_env()).stdout)
    assert status["chunks_in_view"] > 0 and status["main_language"] == "python"
