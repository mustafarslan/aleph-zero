#!/bin/sh
# Starts the Aleph Zero engine's MCP server for the plugin. Which engine: engine.sh.
exec "$(cd "$(dirname "$0")" && pwd)/engine.sh" serve
