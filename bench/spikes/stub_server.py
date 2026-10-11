"""Smallest server that imports what the real one will: MCP, numpy, a grammar."""
import numpy  # noqa: F401  (import cost is part of what is measured)
import tree_sitter_language_pack as pack
from mcp.server.mcpserver import MCPServer

server = MCPServer("aleph-stub", instructions="Stub server for the M0 attach-time spike.")


@server.tool(meta={"anthropic/alwaysLoad": True})
def ping(text: str = "pong") -> str:
    """Echo text back after touching one grammar."""
    pack.get_language("python")
    return text


if __name__ == "__main__":
    server.run("stdio")
