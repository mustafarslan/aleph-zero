# ADR-003: Graph scope and depth

- **Status:** The code graph is built and measured (M7, 2026-10-12), ahead of its gate: it is not offered to the agent by default. The knowledge graph is deferred to M8.
- **Date opened:** 2026-10-11

## Question

Should retrieval results be expanded with call-graph and architectural-graph neighbours, at what depth, and automatically or on request?

## Evidence so far

- Flattened 1-hop context raised task success in RepoGraph (29.67% against a 27.33% baseline); flattened 2-hop lowered it to 26.00% while costing 10.5k tokens.
- In LocAgent, removing graph traversal cost 2 points of file accuracy; removing the lexical index cost 13.
- Syntactic Python call graphs have high precision and low recall: 98.7% / 14.4% (Depends), 74.4% / 20.6% (Pyan). Inter-procedural analysis reaches 69.9% recall (PyCG).
- Only 10.3% of gold functions sit one hop from the entities named in an issue; 74.0% sit within two (KGCompass).

## Direction

- No automatic expansion of search results.
- The graph is an opt-in tool (`references`, callers and callees). One-hop expansion is an explicit `search` parameter, off by default.
- Each graph feature ships only if the M4 A/B improves with it.

## Edges (added 2026-10-11, after the oxn review; see ADR-007)

- **Every edge carries three stamps:** provenance (syntactic, import-resolved, SCIP), resolution level (L0 same-file, L1 cross-file by imports and names, L2 compiler-grade) and confidence (1 over the number of candidates for name-matched edges).
- **Edges are persisted.** oxn computes L1 in memory on every run and stores nothing unless a SCIP index was ingested; Aleph stores L0 and L1 edges so the tools can answer per-symbol queries.
- **Validity depends on the level.**
  - L0 edges depend only on the file's own blob, so they are keyed by blob id and shared across branches and worktrees.
  - L1 edges depend on the file and on what it imports, so they are valid per view. They are stored against the blob together with a fingerprint of the import set they were resolved in, and recomputed when that fingerprint differs.
  - L2 edges follow the rule in ADR-004.
- **The resolver is ported from oxn and extended**: multi-hop re-exports and barrel files (including Rust `pub use`), receiver calls resolved through local constructor bindings and typed parameters, and multi-package layouts (`go.work`, Python workspaces).
- **Private closures and lambdas are not project-wide candidates.**

## Acceptance

Graded against SCIP with oxn's protocol and corpora, per language:

| | oxn v0.1.2 (confident precision / confident recall) | Aleph's bar |
|---|---|---|
| Python | 100% / not reported | precision ≥ 99%, recall reported and above oxn's once measured |
| Go | 99.1% / 64.1% | precision ≥ 99%, recall > 64.1% |
| Rust | 99.1% / 42.3% | precision ≥ 99%, recall > 42.3% |
| TypeScript | 100% / 56.7% | precision ≥ 99%, recall > 56.7% |
| JavaScript | 99.8% / not reported | precision ≥ 99% |
| Java | 100% / not reported | precision ≥ 99% |
| C++ | not supported | reported |

Each of the three resolver improvements is measured separately, so a gain is attributed to its cause.

## As built and measured (M7, 2026-10-12)

### What was built

- **Per file, read once:** call sites as written (name, what it is called on, whether through a path or on a value, what its first name is bound to inside its function, the type the function states for a local receiver), the names its imports bind (aliases, `require`, re-exports, Go package names, Rust `use` trees and `mod`), what its types extend, implement or embed, and the types they state for their fields. Nine languages have readers; six are graded.
- **Per question, worked out:** `graph.py` resolves a call in steps, from what the code says to what only the name suggests: the file's own scope; an import followed to the file that declares the name, through re-exports; the same package; the class the call sits in and what it is built on; the stated type of a parameter, a local or a field; and last, every definition of that name. `aleph callers`, `aleph callees` and the `references` tool ask it.
- **Three shapes get no answer,** because any answer would be a guess that reads as knowledge: a call on a name the function binds itself (a parameter called `filter` is not the project's `filter`), a call through an import that leads outside the repository, and a call on the method's own object when neither its class nor its bases declare the callee.

### Where this differs from the direction above, and why

- **No edge is stored.** The section on edges says L0 and L1 edges are persisted with a fingerprint of the import set. An edge depends on more than a file's imports: on what a re-export two files away leads to, on a base class in a third file, on how many definitions of a name the view holds. No per-file fingerprint says when it is stale. Resolved when asked, a question on Django takes 6 to 94 ms.
- **Nothing is read at indexing time.** Written eagerly, the facts took Django's cold index from 9.1 s to 12.3 s and its index from 116 MB to 153 MB, for every user, asked or not. They are now one compressed row per file, written when a graph question is first asked about a worktree: 3.6 s once for Django's 2,280 code files, 3 MB. Indexing and the chunker version are unchanged; no existing index is rebuilt.
- **The graph holds the files a question reaches, not the view.** Held whole, Django's graph took 58 MB of a session's 130 MB share (ADR-005).
- **Every edge carries level, provenance and the number of candidates,** as planned, and one stamp more: `certain`, below.

### Against the bars

Graded with oxn's protocol on oxn's six corpora at its pinned commits. Confident means one candidate.

| Corpus | Graded calls | Confident precision | Confident recall | oxn's recall | Bar |
|---|---|---|---|---|---|
| go-kit (Go) | 2,081 | 100.0% | 80.7% | 64.1% | met |
| ripgrep (Rust) | 6,901 | 100.0% | 60.2% | 42.3% | met |
| nest (TypeScript) | 9,158 | 100.0% | 80.0% | 56.7% | met |
| httpx (Python) | 1,556 | 100.0% | 87.7% | not reported | precision met |
| eslint (JavaScript) | 15,301 | 100.0% | 48.5% | not reported | precision met |
| spring-petclinic (Java) | 369 | 100.0% | 90.2% | not reported | precision met |

C and C++ are not graded: no SCIP indexer for them is installed.

### What oxn's protocol does not see

The protocol grades only calls whose callee is inside the repository. A call into a library is never graded, so an answer there costs nothing in these figures, and every such answer is a wrong edge. The grader now counts them (sites SCIP places in a package the index does not define). Before types were followed, all but three of the single-candidate answers at library calls were name matches on a value of unknown type: on ripgrep 1,398 of them, mostly `unwrap` and `clone`, against 593 right ones of the same kind inside the repository.

So an edge is **certain** only when it has one target and was not found by name alone. A lone candidate found by name is listed as possible. With library calls counted:

| Corpus | Certain precision | Wrong certain answers | Certain recall | Single name matches at library calls (listed as possible) |
|---|---|---|---|---|
| go-kit | 100.0% | 0 at 3,595 library calls | 76.4% | 35 |
| ripgrep | 100.0% | 1 at 6,333 | 56.4% | 1,213 |
| nest | 100.0% | 0 at 3,327 | 78.9% | 64 |
| httpx | 99.8% | 3 at 1,484 | 82.1% | 95 |
| eslint | 100.0% | 0 at 6,604 | 46.9% | 309 |
| spring-petclinic | 100.0% | 0 at 1,241 | 83.5% | 33 |

The stricter stamp also clears oxn's recall bars (76.4 against 64.1, 56.4 against 42.3, 78.9 against 56.7). The three on httpx are calls of a method named `set` that the oracle resolves to Python's builtin `set`; the one on ripgrep is a `use` inside a function that shadows a function of the same name.

### The three extensions, each switched off alone

Confident recall / certain recall.

| Corpus | All three | Without re-exports | Without packages of the same repository | Without stated types |
|---|---|---|---|---|
| go-kit | 80.7% / 76.4% | 80.7% / 76.4% | 80.7% / 76.4% | 60.1% / 50.9% |
| ripgrep | 60.2% / 56.4% | 54.6% / 50.7% | 52.8% / 48.8% | 53.7% / 45.1% |
| nest | 80.0% / 78.9% | 75.7% / 69.2% | 79.2% / 78.0% | 63.3% / 49.8% |
| httpx | 87.7% / 82.1% | 41.8% / 36.1% | 87.7% / 82.1% | 66.3% / 51.3% |
| eslint | 48.5% / 46.9% | 48.1% / 46.5% | 48.5% / 46.9% | 48.8% / 45.0% |
| spring-petclinic | 90.2% / 83.5% | 90.2% / 83.5% | 90.2% / 83.5% | 89.2% / 24.7% |

Without stated types Go is below oxn's bar (60.1 against 64.1): following the types of receivers and fields is what clears it. go-kit, eslint and petclinic are one package each, so the second column cannot move them.

### On repositories the resolver was not developed against

The six corpora are both what the resolver was developed against and what it is graded on, as in oxn. Three more were indexed here with scip-python and scip-typescript and graded:

| Corpus | Graded calls | Confident precision | Confident recall | Certain precision | Certain recall |
|---|---|---|---|---|---|
| Flask, first run | 476 | 100.0% | 69.3% | 98.1% (6 wrong at 844 library calls) | 63.7% |
| Flask, after the fix | 476 | 100.0% | 69.3% | 100.0% | 63.7% |
| FastAPI, graded once, after the fix | 6,026 | 100.0% | 72.2% | 100.0% (0 wrong at 2,674) | 71.6% |
| routing-controllers (TypeScript) | 273 | 98.2% | 82.4% | 100.0% (0 wrong at 260) | 74.0% |

- Flask's six were one bug: inside the package `flask.json`, `import json` was resolved to the package itself. Fixed; Flask is no longer unseen for that rule, FastAPI is.
- On routing-controllers, confident precision under oxn's protocol is 98.2%, below the 99% bar: four name matches on a variable of an inline type. None is stamped certain. Nothing was changed for them: a rule tuned after seeing them would no longer be tested by this corpus.
- No held-out corpus for Go or Rust is on this machine.

### What is not answered, and what is not known

- **Functions defined inside functions** are not definitions in Aleph (M1), so a call to one gets no answer. That is 36% of eslint's graded calls and most of its distance from the others.
- **httpx:** 27.7% of the call sites SCIP resolves are excluded as untrustworthy by the protocol (scip-python resolves re-exported names to a place that contradicts what is written). oxn reports 19% for the same corpus; the difference was not traced.
- **The oracle is not the truth.** Where SCIP cannot follow a destructured `require` it names the local constant; Aleph's answer, the function in the other file, is graded wrong.
- **Cost:** the first graph question about a worktree reads every code file (3.6 s for Django); after that a question costs 6 to 94 ms on Django beyond finding the view, and the graph keeps about 15 MB once it has touched 500 files. A name mentioned in more than 3,000 chunks is answered from the first 3,000 and says so.

## Decision

- The code graph is built, graded and kept off for the agent: `aleph callers` and `aleph callees` work; the `references` tool is listed only when the server runs with `ALEPH_GRAPH=1`. Whether it ships is decided as this record's direction says, by the A/B run (ADR-011), which is the owner's.
- Acceptance: all bars are met under oxn's protocol, and under the stricter certain stamp. The owner decides which of the two the acceptance is read against (DEFERRED, entry 9); the tools use the stricter one.
- No automatic expansion of search results, as before.
