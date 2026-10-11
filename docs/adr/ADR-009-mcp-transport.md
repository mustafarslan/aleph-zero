# ADR-009: The MCP server is written by hand

- **Status:** Accepted 2026-10-12.
- **Date opened:** 2026-10-12

## Question

ADR-007 left one measurement open: oxn hand-wrote its MCP server; Aleph used the MCP Python SDK in its M0 spikes. Does a hand-written stdio transport cut a session's baseline enough to be worth owning?

## Measured

`bench/spikes/transport.py`, M4 Max, best of three. Both servers expose one tool; "with engine" means the first call also imports Aleph's search code and loads a grammar.

| | Ready after | Idle private memory | After 200 calls | Per call |
|---|---|---|---|---|
| Hand-written, bare | 12 ms | 8.6 MB | 8.6 MB | 0.02 ms |
| Hand-written, with engine | 12 ms | 8.6 MB | 15.7 MB | 0.02 ms |
| MCP SDK, bare | 211 ms | 53.5 MB | 53.5 MB | 0.40 ms |
| MCP SDK, with engine | 212 ms | 53.5 MB | 55.6 MB | 0.41 ms |

The SDK costs about 45 MB of private memory and 200 ms of start-up in every session, before any work. With four sessions that is about 180 MB of the 1,024 MB budget (ADR-005).

## Decision

The server speaks JSON-RPC over stdio itself. It imports neither `mcp` nor `pydantic`. The `mcp` package stays in the development dependencies, because its client is the cheapest conformance check against our server.

## What we now own

The SDK did these for us; each is a test in `tests/test_server.py`:

- Protocol version negotiation in `initialize`: answer with the client's version when we support it, otherwise with our latest.
- `tools/list` with JSON Schema for every tool and `_meta["anthropic/alwaysLoad"]` on `search`.
- A tool that fails returns a result with `isError: true` and a message. JSON-RPC errors are for the protocol only (unknown method, malformed request), because clients treat the two differently.
- `notifications/cancelled` stops a running call.
- `ping`, and ignoring notifications we do not know.
- Nothing but protocol messages on stdout; diagnostics go to stderr.
- Exit when stdin closes; a client that has gone away is not an error.
- Requests the server may send to the client (`roots/list`) are not used: each tool takes `cwd`, and the server's own working directory is the default.

## What would change this decision

A protocol revision that makes stdio servers materially harder to implement (a mandatory streaming or session layer), or a conformance failure against Claude Code that the SDK does not have. The gate for each release is a headless Claude Code session that lists `search` and calls it.
