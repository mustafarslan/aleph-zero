# Changelog

## 0.1.1

- The plugin's `graph` option is free text (`on` or `off`), as Anthropic's plugin directory requires for an option passed
  to the MCP server.
- The plugin folder has an icon, and its README describes each hook, the tools the plugin calls and what it sends where.

## 0.1.0

First public release.

- Hybrid code search (full text with identifier splitting, definitions by name, paths, and
  optional search by meaning with a local embedding model), always matching the working tree.
- Definitions and outlines for Python, TypeScript, JavaScript, Go, Rust, Java, C++ and C.
- Code graph (callers and callees, certain or candidate), with SCIP compiler indexes.
- Architecture facts, decision records, linked sources, and memory across sessions.
- Claude Code plugin, installable from this repository's marketplace, with steering levels
  and a side chat; MCP server for other clients.
- Search by meaning on by default for JavaScript, TypeScript, C, C++ and Rust, by benchmark
  ([ADR-002](docs/adr/ADR-002-lexical-and-fusion.md)).
