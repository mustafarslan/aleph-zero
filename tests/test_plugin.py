"""The Claude Code plugin in plugin/: what Claude Code's own validator and test runner say."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
claude = shutil.which("claude")
needs_claude = pytest.mark.skipif(claude is None, reason="Claude Code is not installed: the plugin cannot be validated here")


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
