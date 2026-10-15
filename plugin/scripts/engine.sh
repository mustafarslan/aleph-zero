#!/bin/sh
# Runs the Aleph Zero engine with the given arguments, for the plugin (its MCP server, /aleph configure).
# Loaded from a checkout of the repository (--plugin-dir, or a marketplace added by local path), the
# engine is the one in that checkout's environment when it has one. Otherwise it is the `aleph-zero`
# command that `uv tool install` puts in uv's tool directory, which is not on PATH for Claude Code
# started from the Dock. Never a bare `aleph` from PATH: TeX Live installs a program of that name.
here="$(cd "$(dirname "$0")" && pwd)"
engine="$here/../../.venv/bin/aleph-zero"
if [ -x "$engine" ]; then
  exec "$engine" "$@"
fi
if command -v aleph-zero >/dev/null 2>&1; then
  exec aleph-zero "$@"
fi
for engine in "${UV_TOOL_BIN_DIR:-}/aleph-zero" "${XDG_BIN_HOME:-$HOME/.local/bin}/aleph-zero"; do
  if [ -x "$engine" ]; then
    exec "$engine" "$@"
  fi
done
echo "aleph-zero: the engine is not installed: uv tool install git+https://github.com/mustafarslan/aleph-zero.git" >&2
exit 127
