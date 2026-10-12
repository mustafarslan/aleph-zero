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

- Protocol version negotiation in `initialize`: answer with the client's version when we support it, otherwise with our latest. Only `2025-11-25` and `2025-06-18` are offered: the two older versions allowed several requests in one message, which this server does not read.
- Request ids are strings or numbers. Anything else (an object, a list, `true`) is refused with -32600, since it can be neither echoed nor tracked; the independent review showed such an id ending the worker thread.
- A cancellation is remembered only for a call that is actually queued, so one for a request never made cannot swallow a later request with that id.
- `tools/list` with JSON Schema for every tool and `_meta["anthropic/alwaysLoad"]` on `search`.
- A tool that fails returns a result with `isError: true` and a message. JSON-RPC errors are for the protocol only (unknown method, malformed request), because clients treat the two differently.
- `notifications/cancelled` stops a running call.
- `ping`, and ignoring notifications we do not know.
- Nothing but protocol messages on stdout; diagnostics go to stderr.
- Exit when stdin closes; a client that has gone away is not an error.
- Requests the server may send to the client (`roots/list`) are not used: each tool takes `cwd`, and the server's own working directory is the default.

## What Claude Code actually sends (2.1.296, recorded 2026-10-12)

1. `server/discover` with id `"server-discover-probe-1"`, proposing protocol `2026-07-28` in `_meta`. This is the newer, 2026 handshake. We answer "method not found" (-32601).
2. On that answer it falls back to `initialize` with protocol `2025-11-25`, then `notifications/initialized`, `tools/list`, and `tools/call` carrying a `_meta` with a progress token.

So request ids may be strings or numbers, and the fallback costs one extra round trip at start-up. Speaking the 2026 handshake natively is deferred until its rules are read from the specification rather than inferred; the fallback is the client's documented behaviour.

## The server as built (M1c)

On Django (7,080 files), `aleph serve` as a child process: ready 45 ms after launch, 16.8 MB of private memory before the first call, 36.7 MB after eighteen searches. A search costs about 84 ms, nearly all of it the git check (ADR-008).

In a headless Claude Code session with only this server attached, `search` is in the tool list and callable at once; `outline`, `symbol` and `status` are listed as deferred, as intended: one always-loaded description is paid for in every session, the others when needed. The SDK's own client lists and calls all four (a test).

## What would change this decision

A protocol revision that makes stdio servers materially harder to implement (a mandatory streaming or session layer), or a conformance failure against Claude Code that the SDK does not have. The gate for each release is a headless Claude Code session that lists `search` and calls it.

## The revision without a handshake (2026-07-28), added 2026-10-12

The [2026-07-28 revision](https://modelcontextprotocol.io/specification/2026-07-28/changelog) removes `initialize`: every request carries its protocol version and the client's capabilities in `_meta`, a server must answer `server/discover` with the revisions it speaks, every result says `resultType`, lists say how long they may be kept (`ttlMs`, `cacheScope`), and `ping` is gone. It makes a stdio server easier to write, not harder: there is no session to keep.

The hand-written server speaks both, request by request: a request that names the new revision is answered in its form; a client that opens with `initialize` is answered exactly as before, `ping` included; a revision the server does not know is refused with the ones it does (`-32022`), which is what lets a client of either kind find common ground. Only a revision in neither list is refused: a request that names a handshake revision in `_meta` is answered as that revision is, and `server/discover` is answered however it is asked, a version named or not (the review of 2026-10-12 found both refused). The protocol's own SDK client, which speaks the new revision, discovers, lists and calls against the server in the tests, next to the SDK's older client.

It is switched on with `ALEPH_MCP_STATELESS=1` and off otherwise. Claude Code (2.1.296) asks for the new revision first and falls back to the handshake when told "method not found": that fallback is what every session uses today and is known to work, and Claude Code itself has not been watched on the new path (that takes a session of the owner's). The default flips when it has.
