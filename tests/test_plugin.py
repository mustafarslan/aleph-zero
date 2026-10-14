"""The Claude Code plugin in plugin/: what Claude Code's own validator and test runner say."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
claude = shutil.which("claude")
needs_claude = pytest.mark.skipif(claude is None, reason="Claude Code is not installed: the plugin cannot be validated here")


def test_the_engine_is_installed_under_a_name_no_other_program_has():
    scripts = (PLUGIN.parent / "pyproject.toml").read_text()
    assert 'aleph-zero = "aleph_zero.cli:main"' in scripts and 'aleph = "aleph_zero.cli:main"' in scripts


def test_the_manifest_and_the_engine_entry_are_what_the_mod_expects():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "aleph-zero"
    assert manifest["userConfig"]["rung"]["options"] == ["0", "1", "2", "3"]
    servers = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]
    assert list(servers) == ["aleph"]                      # the mod calls the server by this name
    assert (PLUGIN / "scripts" / "serve.sh").stat().st_mode & 0o111
    assert json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["modules"] == ["./register.js"]


@needs_claude
def test_claude_code_accepts_the_plugin_with_no_warnings():
    proc = subprocess.run([claude, "plugin", "validate", str(PLUGIN), "--strict"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-500:]
    assert "Validation passed" in proc.stdout and "gating hook without .catch" not in proc.stdout


@needs_claude
def test_the_mods_own_tests_pass():
    if not list(PLUGIN.rglob("*.test.ts")):
        pytest.skip("the mod has no tests yet")
    proc = subprocess.run([claude, "plugin", "test", str(PLUGIN)], capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-1000:]
    assert " 0 fail" in proc.stdout


def _installed_copy(tmp_path: Path) -> Path:
    """The launcher as a marketplace install has it: the plugin copied alone, no checkout's environment beside it."""
    copy = tmp_path / "cache" / "aleph-zero" / "plugin"
    (copy / "scripts").mkdir(parents=True)
    shutil.copy2(PLUGIN / "scripts" / "serve.sh", copy / "scripts" / "serve.sh")
    return copy / "scripts" / "serve.sh"


def _stub(directory: Path, name: str = "aleph-zero", says: str = "stub") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(f"#!/bin/sh\necho \"{says} $*\"\n")
    (directory / name).chmod(0o755)


def test_an_installed_plugin_never_starts_another_program_called_aleph(tmp_path):
    # TeX Live and MacTeX install their own `aleph` (/Library/TeX/texbin/aleph): started with `serve` it waits on
    # standard input, and Claude Code gives up on the server after thirty seconds. Found on the owner's Mac.
    serve = _installed_copy(tmp_path)
    _stub(tmp_path / "texbin", name="aleph", says="tex")
    _stub(tmp_path / "uvbin")
    done = subprocess.run([str(serve)], capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{tmp_path / 'texbin'}:/usr/bin:/bin", "UV_TOOL_BIN_DIR": str(tmp_path / "uvbin")})
    assert done.stdout.strip() == "stub serve"
    _stub(tmp_path / "onpath")
    done = subprocess.run([str(serve)], capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{tmp_path / 'texbin'}:{tmp_path / 'onpath'}:/usr/bin:/bin"})
    assert done.stdout.strip() == "stub serve"


def test_an_installed_plugin_finds_the_engine_where_uv_put_it_when_it_is_not_on_path(tmp_path):
    # Claude Code started from the Dock, or a Mac where `uv tool update-shell` was never run: uv's bin is not on PATH.
    serve = _installed_copy(tmp_path)
    _stub(tmp_path / "uvbin")
    done = subprocess.run([str(serve)], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin", "UV_TOOL_BIN_DIR": str(tmp_path / "uvbin")})
    assert done.stdout.strip() == "stub serve"


def test_an_installed_plugin_says_how_to_install_the_engine_when_there_is_none(tmp_path):
    serve = _installed_copy(tmp_path)
    done = subprocess.run([str(serve)], capture_output=True, text=True, timeout=30,
                          env={"PATH": "/usr/bin:/bin"})          # no HOME: nothing of the real one is looked at
    assert done.returncode != 0 and "uv tool install" in done.stderr


def test_the_graph_tools_are_a_plugin_option_passed_to_the_engine():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["userConfig"]["graph"]["options"] == ["off", "on"] and manifest["userConfig"]["graph"]["default"] == "off"
    server = json.loads((PLUGIN / ".mcp.json").read_text())["mcpServers"]["aleph"]
    assert server["env"] == {"ALEPH_GRAPH": "${user_config.graph}"}


def test_the_repository_is_a_marketplace_and_the_plugin_s_version_is_said_in_one_place():
    marketplace = json.loads((PLUGIN.parent / ".claude-plugin" / "marketplace.json").read_text())
    assert [(entry["name"], entry["source"]) for entry in marketplace["plugins"]] == [("aleph-zero", "./plugin")]
    # An installed copy changes when this version does (a release); said in the manifest only, never also in the entry.
    assert json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
    assert all("version" not in entry for entry in marketplace["plugins"])


def test_the_package_the_plugin_and_the_server_say_one_version():
    # Users get a new plugin only when its version rises; a release raises all three together.
    import tomllib

    from aleph_zero import server
    package = tomllib.loads((PLUGIN.parent / "pyproject.toml").read_text())["project"]["version"]
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
    assert package == plugin == server.SERVER_INFO["version"]


def test_the_plugin_folder_carries_the_license_the_repository_has():
    # The directory installs the plugin folder alone; it must hold its license itself.
    assert (PLUGIN / "LICENSE").read_text() == (PLUGIN.parent / "LICENSE").read_text()
    assert len((PLUGIN / "README.md").read_text().split()) >= 40
