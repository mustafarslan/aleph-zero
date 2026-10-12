---
name: find-code
description: Use when locating code in this repository - where something is implemented, where a name is defined, what a file contains - before reaching for recursive grep or reading whole files
---

# Finding code with Aleph Zero

Aleph Zero indexes this repository as it is on disk right now, uncommitted edits included.

- **`search`**: ask in plain words or with identifiers ("where are refresh tokens rotated", `TokenStore.get`). It returns ranked locations (`path:lines`, kind, name, signature) with snippets. Use it first when you do not know where something lives.
- **`symbol`**: where a name is defined. A qualified name narrows it (`TokenStore.get`).
- **`outline`**: what a file defines, with line ranges, instead of reading the whole file.
- **`status`**: what is indexed, and what is not searched (files outside a sparse checkout, submodules).

Then read only the lines a result points to.

Grep is still right for an exact string you must find every occurrence of (a rename, a log message, a configuration key), and for checking that something is gone.
