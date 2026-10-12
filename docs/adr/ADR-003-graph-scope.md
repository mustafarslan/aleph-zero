# ADR-003: Graph scope and depth

- **Status:** The code graph is built and measured (M7, 2026-10-12), ahead of its gate: it is not offered to the agent by default. The knowledge graph is built and measured (M8, 2026-10-12), behind the same switch; its bar for decision retrieval is not met (equal to oxn's figure, not above).
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
| ripgrep (Rust) | 6,901 | 100.0% | 60.1% | 42.3% | met |
| nest (TypeScript) | 9,158 | 100.0% | 80.1% | 56.7% | met |
| httpx (Python) | 1,556 | 100.0% | 87.5% | not reported | precision met |
| eslint (JavaScript) | 15,301 | 99.9% (4 of 7,426 differ from the oracle) | 48.5% | not reported | precision met |
| spring-petclinic (Java) | 369 | 100.0% | 94.6% | not reported | precision met |

C and C++ are not graded: no SCIP indexer for them is installed.

### What oxn's protocol does not see

The protocol grades only calls whose callee is inside the repository. A call into a library is never graded, so an answer there costs nothing in these figures, and every such answer is a wrong edge. The grader now counts them (sites SCIP places in a package the index does not define). Before types were followed, all but three of the single-candidate answers at library calls were name matches on a value of unknown type: on ripgrep 1,398 of them, mostly `unwrap` and `clone`, against 593 right ones of the same kind inside the repository.

So an edge is **certain** only when it has one target and was not found by name alone. A lone candidate found by name is listed as possible. With library calls counted:

| Corpus | Certain precision | Wrong certain answers | Certain recall | Single name matches at library calls (listed as possible) |
|---|---|---|---|---|
| go-kit | 100.0% | 0 at 3,595 library calls | 76.4% | 35 |
| ripgrep | 100.0% | 1 at 6,333 | 56.2% | 1,242 |
| nest | 100.0% | 0 at 3,327 | 78.9% | 64 |
| httpx | 99.8% | 3 at 1,484 | 81.8% | 95 |
| eslint | 100.0% | 0 at 6,604, and 2 inside the repository | 46.9% | 309 |
| spring-petclinic | 100.0% | 0 at 1,241 | 87.3% | 33 |

The stricter stamp also clears oxn's recall bars (76.4 against 64.1, 56.2 against 42.3, 78.9 against 56.7). The three on httpx are calls of a method named `set` that the oracle resolves to Python's builtin `set`, where Aleph names the method. The two on eslint are classes imported through a destructured `require` of the repository's own package: the oracle names the importing file's constant, Aleph the class. The one on ripgrep is a `use` inside a function that shadows a function of the same name; that one is Aleph's error.

### The three extensions, each switched off alone

Confident recall / certain recall.

| Corpus | All three | Without re-exports | Without packages of the same repository | Without stated types |
|---|---|---|---|---|
| go-kit | 80.7% / 76.4% | 80.7% / 76.4% | 80.7% / 76.4% | 60.1% / 50.9% |
| ripgrep | 60.1% / 56.2% | 54.4% / 50.4% | 52.8% / 48.8% | 53.7% / 45.1% |
| nest | 80.1% / 78.9% | 75.7% / 69.2% | 79.2% / 78.0% | 63.3% / 49.8% |
| httpx | 87.5% / 81.8% | 41.8% / 36.0% | 87.5% / 81.8% | 66.3% / 51.3% |
| eslint | 48.5% / 46.9% | 48.1% / 46.5% | 48.5% / 46.9% | 48.8% / 45.0% |
| spring-petclinic | 94.6% / 87.3% | 94.6% / 87.3% | 94.6% / 87.3% | 89.4% / 24.9% |

Without stated types Go is below oxn's bar (60.1 against 64.1): following the types of receivers and fields is what clears it. go-kit, eslint and petclinic are one package each, so the second column cannot move them.

### Two adversarial reads of the certain stamp

Both asked for realistic sources where a certain edge is wrong. Every case was reproduced before it was fixed, and each fix has a test that failed first.

- **agy (a second model)** gave six: Python's method resolution order through several bases, Go's shallowest embedding, a Java overload inherited past a same-named method (the number of arguments now narrows overloads, which took petclinic's confident recall from 90.2% to 94.6%), two star imports that bring the same name, a module that imports a name for itself and re-exports another of that name through a star, and two Rust types of one name in one crate.
- **An independent reviewer** of the whole branch found the gap the first rule set left open: a method inherited *past a base that is not among the files* (`class MyAdmin(admin.ModelAdmin, ExportMixin)`, a Go struct embedding `sync.Mutex`, a Java class extending a library class). The base that comes first may declare the method itself. Such an answer is now given but not certain, and says why. It also found: a signature too long to have been stored whole deciding between overloads (a regression of the fix above); a value built by a factory the function receives as a parameter taking the type of the file's function of that name; a parameter's stated type surviving a loop that rebinds it; a type parameter `T` read as a type `T` of the package; a loop variable at file level read as the function of its name; a package entered by its declarations instead of its code; and callers missed when the definition reaches them under a name given by a renaming re-export.

None of these occurred in the graded corpora: the figures moved by a few tenths, except Java's. A grade says what the corpora contain, not what the rules get wrong.

### On repositories the resolver was not developed against

The six corpora are both what the resolver was developed against and what it is graded on, as in oxn. Three more were indexed here with scip-python and scip-typescript and graded:

| Corpus | Graded calls | Confident precision | Confident recall | Certain precision | Certain recall |
|---|---|---|---|---|---|
| Flask, first run | 476 | 100.0% | 69.3% | 98.1% (6 wrong at 844 library calls) | 63.7% |
| Flask, after the fix and the two reads above | 476 | 100.0% | 69.3% | 100.0% | 63.0% |
| FastAPI, wrong answers never looked at | 6,026 | 100.0% | 72.2% | 100.0% (0 wrong at 2,674) | 71.6% |
| routing-controllers (TypeScript) | 273 | 98.2% | 82.4% | 100.0% (0 wrong at 260) | 74.0% |

- Flask's six were one bug: inside the package `flask.json`, `import json` was resolved to the package itself. Fixed; Flask is no longer unseen for that rule, FastAPI is.
- On routing-controllers, confident precision under oxn's protocol is 98.2%, below the 99% bar: four name matches on a variable of an inline type. None is stamped certain. Nothing was changed for them: a rule tuned after seeing them would no longer be tested by this corpus.
- No held-out corpus for Go or Rust is on this machine.

### What is not answered, and what is not known

- **Functions defined inside functions** were not answered in M7 (36% of eslint's graded calls). They are since M7b: see "Inner functions" below. They are still not found by `symbol` or shown by `outline`, and `callers` of one is not answered.
- **httpx:** 27.7% of the call sites SCIP resolves are excluded as untrustworthy by the protocol (scip-python resolves re-exported names to a place that contradicts what is written). oxn reports 19% for the same corpus; the difference was not traced.
- **The oracle is not the truth.** Where SCIP cannot follow a destructured `require` it names the local constant; Aleph's answer, the class or function in the other file, is graded wrong.
- **Cost:** the first graph question about a worktree reads every code file: 3.6 s for Django's 2,280 code files, of which the readers themselves are about 1.8 s and the rest is reading the files from git and writing the rows. After that a question costs 6 to 94 ms on Django beyond finding the view, and the graph keeps about 15 MB of Python objects once it has touched 500 large files (the limit it keeps loaded). A name mentioned in more than 3,000 chunks is answered from the first 3,000 and says so.
- **Callers that are not found:** a caller that reaches a definition through a default export under a name of its own (`import s from './util'`), or under a name built at run time, does not mention the definition's name, and the text index is what finds callers. A name with no letter or digit in it (`$`) cannot be looked up; the answer says so.
- **Linked sources:** an absolute Python import inside a linked repository is not followed.

## Inner functions (M7b, 2026-10-12)

**What changed.** A call to a function defined inside the function it is called from, or to a local name bound to a function written there (`const f = () => …`, `f = lambda: …`, `f := func() {…}`, `let f = |x| …`), is answered with that function: same file, by syntax. It is certain when the nearest scope that binds the name binds it once, to a function, and nothing in the file gives the name another value (an assignment, also by destructuring; in Python a `nonlocal` or `global` declaration of it, or `del`); a name given another value, or defined on both sides of an `if`, is answered and not stamped certain. A name also bound as a parameter, by an import inside the function, or to anything that is not a function, is not answered, as before. A method of a class, an `impl` or a module that is itself defined inside a function is not that function's inner function. The plan had said a `let`, `var` or `let mut` name would never be certain; what was built looks for the assignment instead, which also takes the stamp from a `const`-like name that is assigned to and leaves it on a `let` that never is. The stamp says where the name is defined, not that the call can run: a call written above its definition is answered the same. Only the code graph changed: its per-file facts are read again on the next graph question (they are filled lazily), and nothing is indexed or embedded again. An inner function is a target and nothing else: `symbol` and `outline` print what they printed, and `callers` of one is not answered (whether they should show there is the owner's, DEFERRED entry 9).

**Measured**, by the plan written before the code: the same harness, protocol and nine corpora as above, the baseline run again first (it reproduced, but for one go-kit site that a grader fix made a minute after M7's result file now matches). Confident precision / confident recall, then certain precision over all sites / certain recall; the six first, the held-out three once afterwards (and again when the grader gained a count and after the review's fixes, with the same figures; see below).

| Corpus | Confident before | Confident after | Certain before | Certain after | Inner answers (correct of confident) |
|---|---|---|---|---|---|
| go-kit | 100.0 / 80.7 | 100.0 / 81.7 | 100.0 / 76.4 | 100.0 / 77.4 | 20 of 20 |
| ripgrep | 100.0 / 60.1 | 100.0 / 60.7 | 99.97 / 56.2 | 99.97 / 56.9 | 44 of 44 |
| nest | 100.0 / 80.1 | 100.0 / 82.5 | 100.0 / 78.9 | 100.0 / 81.3 | 222 of 222 |
| httpx | 100.0 / 87.5 | 100.0 / 89.7 | 99.76 / 81.8 | 99.77 / 84.0 | 34 of 34 |
| eslint | 99.95 / 48.5 | 99.97 / **81.3** | 99.97 / 46.9 | 99.98 / 78.4 | 5,012 of 5,012 |
| petclinic | 100.0 / 94.6 | unchanged | 100.0 / 87.3 | unchanged | none |
| fastapi (held-out) | 100.0 / 72.2 | 100.0 / 73.1 | 100.0 / 71.6 | 100.0 / 72.4 | 48 of 48 |
| routing-controllers (held-out) | 98.22 / 82.4 | 98.28 / 85.0 | 100.0 / 74.0 | 100.0 / 76.6 | 7 of 7 |
| flask (held-out) | 100.0 / 69.3 | **99.18** / 76.9 | 100.0 / 63.0 | **99.11** / 70.6 | 33 of 36 by the protocol |

- **Flask's three.** The plan's condition was that no precision figure falls, and Flask's fell by three answers. Read in the source and in the index: three test methods of one class each define a function holding a function `generate` (or `gen`) and call it a few lines below. Aleph answers each call with the function defined just above it. scip-python names all of them with one symbol (it leaves the functions around an inner function out of the name), so that symbol has three definitions, and the grader's truth is the first of them, whichever is called. The figure is reported as measured; no rule was changed for it, because the only change that would satisfy the grader is to answer with the wrong function. The grader now counts apart the answers it calls wrong that sit at a further definition of the very symbol the index names: 3 on Flask, none on the other eight.
- **ripgrep** also has 7 calls answered with several candidates (a name bound to a closure twice in one function; in Rust the later `let` shadows the earlier): not confident, not certain, 4 of the 7 with the right one first.
- **The rule check** (each rule of the resolver broken in turn) catches all 39 of M7's mutations and the two added for this rule.
- **After the independent review.** It found one wrong certain answer the corpora do not contain: a method of a class defined inside a function was taken for that function's inner function and displaced the file's own function of that name. Fixed, with three smaller holes in the stamp (an import inside the function, a Python `nonlocal` rebinding, a destructuring assignment), each by a test that failed first. All nine corpora were graded again: no precision figure moved; go-kit gained two answers (`var f = func…`) and eslint 50 certain stamps (giving a function a property is no longer taken for assigning to it), which the table shows. The held-out corpora have therefore been run more than the once the plan said: twice for the resolver (before and after the review's fixes, the same figures) and once more when the grader gained its separate count.

## The knowledge graph as built and measured (M8, 2026-10-12)

### What was built

- **Facts a file states** (`aleph facts`, the `facts` tool). Read from the syntax of one file, each with its file and line: routes (Express and its relatives, FastAPI, Flask, net/http, chi, gin, Spring), where routers are mounted, models and tables (SQLAlchemy, Django, Alembic, JPA, Prisma), environment names read, set or declared, Celery tasks, queues (a Celery `queue=`, a Redis list or channel named by a literal), gRPC services and methods, and the layers and contracts of an `oxn.yaml`. Kept per blob in a table that is filled when facts are first asked of a view (schema 9, brought up to date in place): an index that is only searched pays nothing.
- **Decisions** (`aleph decisions`, the `decisions` tool). Whole records ranked by the words of a task. With a file (`--for PATH`), the records whose `applies-to` covers it, and the layer it is in with what that layer may and may not depend on.
- **What is not done, on purpose.** A route's path is as written where it is registered, with the prefix its own file gives its router. A prefix given where a router is mounted in another file is a `mount` fact of its own and is not joined to routes: which routes pass through a mount is often decided by a router handed from one file to another, and joining them would often be a guess. A path built at run time is not reported. Whether `x.get("/users", y)` registers a route or looks something up is not in the syntax of the call, so it is read as a route only when `x` is known for a router: the file makes it one (`express()`, `Router()`, `new Hono()`, `FastAPI()`, `APIRouter()`, `Blueprint()`) or its name says so (`app`, `router`, `server`, `apiRouter`, `users_router`, `bp`); a router under another name that reaches a file as an argument is missed. In Go a route needs something that can be a handler after the path and a holder not named for a client or a store. `publish` and `subscribe` count as a broker's only on an object named for one (`redis`, `nats`, `mqtt`, `kafka`, `amqp`): an event bus inside the process has the same methods. A bare `environ` is the process's only when the file imports it from `os`. The first version read these more widely; the independent review showed key-value lookups, outbound requests and in-process buses reported as routes and queues, on shapes the two measured repositories do not contain. `oxn.yaml` is read by a reader for its two sections in the forms oxn writes; Aleph takes no YAML parser for one optional file, and a form the reader does not know states nothing.

### Facts, measured

The protocol was fixed in the plan before any fact was extracted. Two of the owner's repositories, on scratch clones (Aleph's index is never written into them); names stay in git-ignored result files.

| Kind | private repository A (1,315 files) | private repository B (733 files; readers unchanged, not seen before) |
|---|---|---|
| Routes of the documented API (OpenAPI, 61 operations) | 61 of 61 | no document |
| Routes, read against the source | Express 50 of 50 sampled; Python 50 of 50 sampled | all 23 (net/http), 23 of 23 |
| FastAPI decorators | 116 of 116 (a search finds 120; 4 are examples inside docstrings) | none |
| Prisma models, Alembic tables, Celery tasks | 28 of 28, 4 of 4, 35 of 35 | none |
| Environment names of the example files, found read in code | **187 of 197 (0.949)** in the registered run; 192 after two shapes were added | no example file |
| Environment reads, read against the source | 49 of 50 sampled (one assignment was called a read) | 30 of 30 sampled; Go reads 23 of 23 a search finds |
| gRPC services and methods | none | 1 of 1, 16 of 16 |
| Queues | 13 of 13 a search finds | none |

- **The ten names not found in the registered run, each looked at:** five were read in two shapes the readers did not know (`process?.env?.X`, `"X" in process.env`), two are read through a constant that holds the name (not readable from the syntax of the read), one occurs only in a comment, two occur nowhere. The two shapes and the assignment-as-read were fixed with failing tests before private repository B was looked at; 0.949 is the registered figure and 192 of 197 is from readers fixed on what this repository showed.
- **Queues** were added after the registered run (the spec asks for broker topics and the plan had left them out), and the readers were written with the 13 lines' shapes already seen: a check, not a held-out figure. The producer in TypeScript and its consumer in Python come out under one name.
- **Layers, contracts and record scope** were checked on oxn's own repository (a local clone): 6 of 6 records' `applies-to` with 16 of 16 patterns, 7 of 7 layers with 33 of 33 patterns, its one contract with its order. A path that two layers name is in the first of them, as oxn has it.
- **After the independent review** the readers were narrowed as described above and a Flask converter (`<int:id>`) that came out mangled was fixed; both repositories were graded again and every figure in the table is unchanged.
- **Measured and not measured.** Measured on real code: Express, FastAPI, Prisma, Alembic, SQLAlchemy models, Celery, environment reads in JavaScript, TypeScript, Python and Go, Go's net/http, gRPC, Redis lists. Exercised by unit fixtures only: Flask, Django models, Spring and JPA, chi and gin, environment reads in Rust and Java. Not read at all: class-based Express (`this.router.get`), `app.all`, NestJS decorators, Django URL patterns.
- **Limits of this measurement.** Two repositories, both the owner's. The precision readings are mine, not independent of the readers' author. The oracles other than the OpenAPI document are searches of the same text: they can show a miss or an invention, not much more. oxn has no figure for any of these.

### Decision retrieval, measured

On oxn's 100 citation pairs and by its protocol (a commit that cites a decision is the task; the cited record is the answer). oxn's BM25: 0.610 / 0.820 / 0.726 (precision at 1, at 3, MRR); the bar is precision at 3 above 0.820.

| Arm | P@1 | P@3 | MRR | Tokens of the answer |
|---|---|---|---|---|
| oxn's ranker in this harness (calibration) | 0.610 | 0.820 | 0.726 | |
| Aleph's ordinary search, a record at its best chunk, words | 0.400 | 0.680 | 0.575 | 1,450 |
| The same with vectors | 0.510 | 0.770 | 0.651 | |
| `aleph decisions`, words alone (first run) | 0.610 | **0.820** | 0.726 | 355 |
| `aleph decisions` with stems (second run, on the owner's instruction) | 0.560 | **0.820** | 0.699 | 355 |
| `aleph decisions`, words and one vector per record (third run, 2026-10-12; the default since) | 0.580 | **0.840** | 0.717 | 348 |

- **Equal, not above.** Ranking whole records by words alone is oxn's function: the same tokens, the same BM25. It scores oxn's figure to the digit, also on the 44 pairs whose task does not restate the record's title (0.545 / 0.773 / 0.680), where a match is not handed over by the title. The answer is a quarter of what chunk search returns for the same question.
- **The second attempt had its limit written before any number.** Three candidates (words reduced to stems; BM25's length normalisation lowered to 0.4; both), chosen on 70 development pairs from two repositories that are not among the graded five, one run on the 100, no third. Stems were chosen (one pair more at 3 on the development pairs) and did not carry over: still 82 of 100 at 3, fewer at 1. They are not adopted; the stemmer and the switch stay in `decisions.py`, tested and off, so the comparison can be repeated on other data.
- **Why the development pairs could not choose.** Their repositories have 6 and about 11 records; the graded ones have up to several dozen. Only six of the 70 miss at 3. No further pairs exist on this machine (the owner's four repositories and the benchmark clones were checked).
- **Above the bar at 3, by two pairs; below at 1, by three (third run).** The ranker was chosen first on other data: 89 citation pairs from three public repositories (cosmos-sdk 40, cometbft 35, backstage 14; cloned on the owner's word; the mining rule tightened and frozen before any score), with each repository held out in turn. There, 48 word-only settings (field weights, the task's first line, b, stems) gained nothing (0.674 against 0.685 at 3), and oxn's ranker and Aleph's by words agreed on all 89 pairs. One vector per record, its cosine and the BM25 score each scaled to 0..1 over the records and added half and half, gave 0.551 / 0.787 / 0.681 against 0.517 / 0.685 / 0.631: +10.1 points at 3, 90% interval +5.6 to +15.7. Frozen in a commit, checked to rank every development pair in the engine as in the measurement, then run once on the 100: 84 of 100 at 3 (four found that words miss, two lost), 58 at 1 against 61, MRR 0.717 against 0.726. On the 44 pairs that do not restate a title: 0.841 at 3 against 0.773. Two pairs in a hundred are within chance; what carries the claim is the held-out result. It is Aleph with vectors against oxn's BM25: oxn has no vectors. Without a running worker the answer is the words-alone one, as before.
- **What this measurement does not settle.** The weight chosen (half words, half nearness) is the lowest the grid allowed, and both variants improved towards it: a lower weight for words may be better and was not tried, because the grid was fixed before the scores. 89 pairs from three repositories, one with 14, is a small development set; 250 to 350 were asked for. One repository of the four cloned (edx-platform) writes its records in reStructuredText, which Aleph does not read as decision records.
- **Fusing with vectors by rank over chunks** (the earlier attempt) was no better on the development pairs (0.714 / 0.900 / 0.821 against 0.743 / 0.914 / 0.834; from the ledger, the result file was not kept) and was not run on the 100.

## Decision

- The code graph is built, graded and kept off for the agent: `aleph callers` and `aleph callees` work; the `references` tool is listed only when the server runs with `ALEPH_GRAPH=1`. Whether it ships is decided as this record's direction says, by the A/B run (ADR-011), which is the owner's.
- Acceptance: all bars are met under oxn's protocol, and under the stricter certain stamp. The owner decides which of the two the acceptance is read against (DEFERRED, entry 9); the tools use the stricter one.
- No automatic expansion of search results, as before.
- The knowledge graph (M8) is built and kept behind the same switch: `aleph facts` and `aleph decisions` work; their tools are listed only with `ALEPH_GRAPH=1`. Its bar for decision retrieval is not met: equal to oxn's 0.820 in two runs. What to try next is the owner's choice (DEFERRED, entry 12).
