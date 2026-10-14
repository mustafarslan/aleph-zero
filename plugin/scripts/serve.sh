#!/bin/sh
# Starts the Aleph engine for the plugin. Loaded from a checkout (--plugin-dir), the engine is
# the one in that checkout's environment. Installed from the marketplace, the plugin is a copy
# on its own: the engine is `aleph` on PATH, or where `uv tool install` put it, which is not on
# PATH for Claude Code started from the Dock or on a Mac where `uv tool update-shell` was never run.
here="$(cd "$(dirname "$0")" && pwd)"
engine="$here/../../.venv/bin/aleph"
if [ -x "$engine" ]; then
  exec "$engine" serve
fi
if command -v aleph >/dev/null 2>&1; then
  exec aleph serve
fi
for engine in "${UV_TOOL_BIN_DIR:-}/aleph" "${XDG_BIN_HOME:-$HOME/.local/bin}/aleph"; do
  if [ -x "$engine" ]; then
    exec "$engine" serve
  fi
done
echo "aleph-zero: the engine is not installed. In a clone of the repository: uv tool install ." >&2
exit 127
