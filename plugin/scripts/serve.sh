#!/bin/sh
# Starts the Aleph engine for the plugin. The plugin lives in the engine's own checkout,
# so the engine is the one in that checkout's environment; failing that, one on PATH.
here="$(cd "$(dirname "$0")" && pwd)"
engine="$here/../../.venv/bin/aleph"
if [ -x "$engine" ]; then
  exec "$engine" serve
fi
exec aleph serve
