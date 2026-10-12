# Deferred work and things waiting for a decision

Things that are not being done now on purpose. Each entry says what it is, why it waits, and how to start it. Newest decisions first. Remove an entry when it is done.

## 0. The A/B run that decides whether v1 ships (spends Claude plan usage)

- **What:** `bench/ab/run.py` runs Claude Code headless on Loc-Bench issues with and without the plugin and compares tokens, tool calls and whether the right files were named (ADR-011). The design's gate: the best rung gives at least 20% fewer tokens or tool calls than no plugin, with no drop in hit rate.
- **Why it waits:** every session spends your plan usage. A pilot of eight sessions (about $1.85) proved the harness and showed the cost: about $0.30 a session on the mid model, so 60 sessions are about $20. It also showed that rung 1 as first written was followed in only two sessions of four, and that run-to-run differences of 20 to 40% occur without Aleph being called at all (ADR-011). The full run is yours to size and start.
- **Decisions that are yours:**
  1. *The baseline.* The gate says "than no plugin"; the design's ladder calls L0 "the engine without the mod". `none` answers whether Aleph helps at all, L0 what the steering adds.
  2. *The shape.* Four rungs at 30 tasks each cannot read a 20% gate (run-to-run noise is about a factor of two; ADR-011). Two arms with 60 tasks, or 30 tasks twice, come closer for the same number of sessions.
  3. *The model.* The pilot used the cheap and the mid model. The gate should be read on the model you work with.
  4. *The ten tasks from your own repositories* that the design asks for need your questions (as entry 2 below).
- **Arms:** `none`, `L0`, `L1n` (rung 1 without the pointer in Grep and Glob: what the pilot ran), `L1`, `L2`, `L3`. Running the same command twice appends a second run of the same tasks; the report then pairs on the mean of a task's runs.
- **How to start it** (example: no plugin against rung 1, 30 tasks, the mid model, at most 60 sessions):

      uv run --group bench python bench/ab/run.py run bench/data/benchc --arms none,L1 --count 30 --model sonnet --max-sessions 60

  Results are appended to `bench/data/results/ab/`, one line per session, so a run can be stopped and its report made from what is there: `bench/ab/run.py report <file>`.
- **Also yours, one minute:** run `claude --plugin-dir plugin` once in this repository, interactively. Claude Code then writes the type declarations for your build into `plugin/.claude-plugin/types/`; the mod was written without them (ADR-011). And allow the engine's tools (`"mcp__plugin_aleph-zero_aleph__*"` in `permissions.allow`), or rungs 2 and 3 add nothing.
- **Status:** harness and pilot done; full run not started.

## 0b. Two minutes with the side chat (it has never been seen)

- **What:** the side chat (ADR-012) was built and tested without an interactive session; nobody has looked at its pane.
- **How:** in this repository run `claude --plugin-dir plugin`, then `/aleph`. Check: the pane opens (to the right in a wide fullscreen terminal, above the prompt otherwise); a question typed into it is answered with `path:line` citations; the model and effort pickers work from the keyboard; **Insert into prompt** drafts a wrapped note into the prompt box and sends nothing; and after closing the pane the transcript shows no trace of the question or the answer. `/aleph check model` says whether the chat can call the model at all.
- **If the pane's command leaves a row in the transcript:** the design's fallback is a button in the band above the prompt instead of a command. Headless, a mod command appended no row.
- **After that:** a week of real use decides whether the next steps are worth building (block toggles, inserting a selection, a stale check before insert). The mod counts how often answers cite a location and how often they are inserted.

## 1. The long GPU run for Benchmark C (decides whether semantic search is on by default)

- **What:** embed the large Loc-Bench repositories so the Python benchmark has about 400 questions instead of 235. This decides Python only (see 1b for the other languages). The gate is "search with vectors beats search by words and names by at least 5 points of file recall@10, with an interval that excludes zero" (M2 plan, Task 8). With 235 questions the interval is roughly ±6 points; with 400, roughly ±4.5.
- **Why it waits:** it keeps the GPU busy for a long time. The first estimate was about a day. That was made while the worker was degrading with uptime and before data files were excluded from embedding; with both fixed it is probably 6 to 12 hours, but that is a guess until one large repository has been timed. It also fetches these public repositories from GitHub (several GB): vllm, pandas, scikit-learn, transformers, ray, scipy, keras, sympy, qiskit, ultralytics, matplotlib, numpy, mlflow, phidata, streamlit, pytorch-lightning, checkov, pypdf.
- **What it is for now: the held-out check of Python's verdict.** On the 235 questions run so far vectors add +5.5 points on issue titles (interval +1.3 to +9.8), which passes the gate. But the candidate pool that reaches it (300) was chosen on those same questions; at the first pool (50) the gain was +3.2 and the gate was not met (ADR-002). The 165 questions in the repositories below have not been seen: if they also show a gain near or above 5 points, the verdict stands; if not, it should be switched off (`src/aleph_zero/dense_policy.py`, one line).
- **Heat:** `prepare` looks at the chip's temperature after every batch and pauses while it is at 95 °C or above, or while macOS reports thermal pressure, until it is back under 80 °C (`--temp-limit` changes the limit); its last line says the highest reading and how often it paused. To watch from another terminal: `uv run python bench/tools/temps.py --watch 30`. On this machine the CPU cores and the GPU share one die and there is no separate GPU sensor, so the die's sensors are what is watched. During the 30-minute stress runs with the worker on the GPU the die stayed at about 62 °C.
- **How to start it** (it can be interrupted and resumed; run it when the machine is otherwise idle, and do not run the test suite at the same time):

      uv run --group bench python bench/eval/benchmark_c.py prepare bench/data/benchc --repos vllm-project/vllm,pandas-dev/pandas,scikit-learn/scikit-learn,huggingface/transformers,ray-project/ray,scipy/scipy,keras-team/keras,sympy/sympy,Qiskit/qiskit,ultralytics/ultralytics,matplotlib/matplotlib,numpy/numpy,mlflow/mlflow,phidatahq/phidata,streamlit/streamlit,Lightning-AI/pytorch-lightning,bridgecrewio/checkov,py-pdf/pypdf

      uv run --group bench python bench/eval/benchmark_c.py run bench/data/benchc --out bench/data/results/benchmark_c.json

  A commit with more than 250,000 chunks is skipped (`--max-chunks`); the last line of `prepare` says how many were skipped or left partly embedded.
- **Status:** not started. Python's verdict is provisional until it runs. Waiting for the owner's go-ahead.

## 1b. Benchmarks for the languages other than Python

- **What:** the gate is read per language (ADR-002), and Loc-Bench is Python only. For C++, C, Go, Java, JavaScript, TypeScript and Rust there are no questions yet, so those languages have no verdict and search by meaning stays off for projects written mainly in them.
- **Order:** by the projects Aleph is activated on. Today that is Python (aleph-zero, oxn, private repository C, and 63% of aeon), then C++ (35% of aeon). The others wait until a project in that language is activated.
- **Question sets found, not fetched:** SWE-bench Multilingual (`SWE-bench/SWE-bench_Multilingual` on Hugging Face: 300 questions, about 12 to 38 per language) and Multi-SWE-bench (`ByteDance-Seed/Multi-SWE-bench`: more repositories per language, among them fmt, nlohmann/json, simdjson, Catch2 and cpp-httplib for C++). Thirty-five questions per language cannot resolve 5 points, so both sets are needed per language.
- **What it costs:** the benchmark harness needs an adapter for each set (the gold files are the files in the fix's patch); then, per language, fetching five to ten public repositories and some hours of GPU embedding.
- **Status:** not started. C++ is next after the Python verdict, with the owner's go-ahead, since it is another GPU run.

## 1c. Tuning runs that need the GPU for hours

- **What:** two things the M2 plan lists and that were not run. (a) The split-threshold sweep of ADR-001: chunk at 96, 128, 192, 256 and 384 tokens and compare on Loc-Bench, with a signature-only control. (b) Re-tuning M1's starting values against Benchmark C: the column weights of the text index, the path and name rules, how many words a long query keeps.
- **Why it waits:** every setting of (a) changes the chunks, so all 235 base commits must be re-indexed and re-embedded per setting: about 1.5 hours of GPU each, five settings. (b) needs no GPU for most settings but a re-index for the column weights.
- **Status:** not started. Ranking and chunking run on the starting values, which passed the gate provisionally as they are (entry 1).

## 2. Fifty hand-written questions for Benchmark C

- **What:** the design asks for at least 50 questions in three categories (conceptual, exact identifier, mixed) on the owner's own repositories.
- **Why it waits:** questions written by the system's author favour the system. They should come from the owner, or be approved by the owner.
- **How to start it:** a file of questions with, for each, the repository, the files that answer it, and the category. The harness can then score them like Loc-Bench instances.
- **Status:** that part of the benchmark is reported as missing.

## 3. Git's untracked cache in the owner's repositories

- **What:** `git config core.untrackedCache true`, per repository.
- **Why it waits:** it changes the owner's git configuration, so Aleph does not do it. It takes the per-query git check from about 74 ms to 26 ms on a 7,000-file repository (ADR-008). `aleph doctor` shows whether it is on.

## 4. The upstream report for llama.cpp

- **What:** `docs/upstream/llama-cpp-logits-reservation.md` is a drafted issue (an embeddings-only context reserves a logits buffer: about 1 MB per token with this model). Aleph carries the one-line patch.
- **Why it waits:** posting it is the owner's to do.
- **To add before filing:** the second finding from M2, now measured (`bench/spikes/worker_uptime.py`, ADR-005): with the server's default prompt cache an embeddings server grows by about 0.8 KB per request and slows down (after 4.5 hours: a tenth of the speed and 1,008 MB). Aleph switches the cache off; upstream may want embeddings mode to do so by default.

## 5. Fixes proposed for Aeon v4

- **What:** the list in ADR-006 (multi-process safety, id reassignment on replay, index rebuilt before replay).
- **Why it waits:** nothing is committed in the Aeon repository without the owner's go-ahead.
- **This is what blocks the next milestone.** M0 to M5 are merged (2026-10-12). M6, the memory layer, is next, and ADR-006 lists three changes Aeon needs before Aleph's first write: heavy dependencies moved into optional extras (a plain install must not download PyTorch), the write-ahead log replayed before the search index is rebuilt, and event ids kept across a replay. With your go-ahead they are made on a branch off `v4` in the Aeon checkout; M6 also needs its own short design first (ADR-006 says so), which I can draft for you to read without touching Aeon.
- **Aleph's side is built** (M6a, 2026-10-12): the catalogue, recall with staleness, the adapter (tested against the extension the M0 probe built out of tree, without touching the Aeon checkout), the `recall` and `remember` tools, `aleph memory`, and the side chat's writes. What waits for you is the three Aeon changes and the design's five decisions; until then memory works, by words and recency, and by meaning where `ALEPH_AEON_LIB` points at a build.
- **After M6:** M7 to M10 (code graph, knowledge graph, SCIP, public release) each *ship* only if the A/B measurement improves (entry 0). M7 is built ahead of that gate (2026-10-12): its tools stay on demand and nothing is added to search until the measurement says it pays. What it leaves for you is entry 9.

## 6. An upper bound on tree-sitter in oxn

- **What:** oxn requires `tree-sitter>=0.25` with no upper bound; 0.26.0 crashes on large trees in a way Aleph pins against (ADR-007). oxn 0.1.2 was not observed to crash.
- **Why it waits:** the oxn checkout is not to be modified from here.

## 7. Left out of M3 on purpose (ADR-010)

- **Searching a directory that is not a git repository.** Today it gets one clear sentence (`git init`). Doing it properly needs its own way to list, hash and watch files, and a rule against walking a home directory. Planned for M10; say if you want it sooner.
- **Things that need Linux:** file names that are not valid UTF-8 (cannot be created on macOS); reading a session's own memory there is written (`/proc/self/smaps_rollup`) but has only been run on macOS; the network-filesystem check reads `/proc/self/mountinfo` and is likewise untested there; a file-system watcher, if it passes the "change, then ask at once" test.
- **Two installed versions of Aleph with different chunkers sharing one index** took turns re-indexing everything. *Fixed in M10:* the older one now stops with one sentence saying a newer version has read this content, as it does for a newer schema; the newer one reads older content again once, as before.
- **The vector file never shrinks.** Rows of collected content are used again, so it stops growing, but after a very large branch is abandoned the file stays at its high-water mark (150 MB per 300,000 chunks). Deleting `vectors.*.f16` and running `aleph embed run` rebuilds it.
- **A trigram index for text written without spaces** (Chinese, Japanese, Korean): no repository in use needs it.
- **Plain values of object literals** (`limit: 10`) are not listed as symbols; functions in them are.
- **Left as they are after the M3 review, with their cost:** vector rows are flushed with `fsync`, not macOS's stronger `F_FULLFSYNC`, so after a power failure (not a crash) a row could hold older bytes than the index says; a cache directory on a network home directory is not detected; a collection round killed half-way waits a day for the next; while a session's own background thread is embedding, each of its queries re-checks the whole view (about 2 ms on Django).

## 8. Carried to later milestones (recorded in the design)

- M4, still open: the 2026 MCP handshake (the engine still relies on the client's fallback to `initialize`); L4 of the steering ladder, if the measurement leaves a gap.
- M2, still open: whether one query prefix serves both code and documentation (needs questions whose answers are documentation).

## 9. The code graph (M7): what waits for you

- **Switching it on for the agent.** `aleph callers` and `aleph callees` work now. The `references` tool is listed to the agent only when the server runs with `ALEPH_GRAPH=1`, because ADR-003 ships a graph feature only if the A/B run improves with it, and that run is yours (entry 0). To try it in a session: set the variable where the plugin starts the server. To measure it: an arm of the A/B harness with the variable set. When it is switched on for good, the server's instructions to the agent should also say when to prefer it: they do not mention it today.
- **Which bar the acceptance is read against.** Under oxn's own protocol Aleph passes all of ADR-003's bars. That protocol does not count calls into libraries; counting them, an answer found by name alone is wrong often enough (on ripgrep about three times in four) that Aleph does not call it certain. ADR-003 reports both. I recommend the stricter reading: it also passes, and it is what the tools show.
- **Functions defined inside functions.** They are not definitions in Aleph (a decision of M1 that outline and symbol follow), so a call to one gets no answer: 36% of eslint's graded calls. Recording them would change what `symbol`, `outline` and the chunker produce and re-chunk every index. Your call; nothing here needs it. *(2026-10-12: calls to them are now answered by the code graph alone, see entry 12; the question about `symbol` and `outline` stands.)*
- **A held-out corpus for Go and Rust.** The resolver was developed against the six corpora it is graded on. Python and TypeScript were also graded on repositories it had never seen (ADR-003). For Go and Rust no such corpus is on this machine; making one needs the network (clone a repository, run its SCIP indexer).
- **C and C++.** The readers exist; nothing grades them (no SCIP indexer for them is installed).

## 10. M8: which repositories (answered 2026-10-12)

- **Asked:** which of your repositories and frameworks the knowledge graph should start with.
- **Your answer:** aeon, private repository D, private repository B or private repository A, my choice.
- **Chosen: private repository A, with private repository B second.** private repository A has what the extractors are for, in two framework families: an Express server with Prisma, a FastAPI service with Alembic, Celery and Redis, a React front end that calls the server's routes; about 339 routes, 13 models, 5 migrations, over a thousand environment reads, two decision documents. private repository B (Go, gRPC, 19 routes) checks that the extractors are not shaped by one repository. aeon has routes and migrations but little else; private repository D is research code with no such structure. All four have your own account as origin.

## 11. Compiler indexes (M9): what is left for you

- **TypeScript is not measured.** nest's dependencies do not install (`npm ci` ends in a dependency-resolution error, with or without the network), and I tried no other flags. Another TypeScript repository whose dependencies install would settle it: name one, or allow `--legacy-peer-deps` for nest.
- **JavaScript is not run by default.** scip-typescript took 2,295 MB on eslint against the 2,048 MB registered beforehand; I did not move the limit after seeing the figure. It runs when you name the limit: `ALEPH_SCIP_MEMORY_MB=3072 aleph scip run --lang javascript`. (Until 2026-10-12 this line said so and the code refused anyway; fixed, ADR-004.)
- **The trust rule is decided** (you asked agy and the advisor): it stays as strict as ADR-004 wrote it, with a hint how to refresh. agy also wanted a weaker stamp for out-of-date answers; measured on Flask it would add 4 calls in 100, so it is not built. ADR-004 has the three views and the figures.
- **Like M7, off for the agent** unless `ALEPH_GRAPH=1`.

## 12. M8, and where Aleph is not ahead of oxn (2026-10-12)

You asked that Aleph be better than oxn in every way, within a limit so that it ends. Each row of ADR-007 was looked at again; ADR-007 ("Where each row stands") has the figures. Three rows are not ahead. Each needs a choice that is yours.

- **Finding the decision that governs a change: equal, not better (0.820 at 3, twice).** By words alone Aleph's ranker and oxn's are the same function. The one change that helped on development data (stems) did not help on the 100 pairs, and I held to the limit written before that run: no third run on those pairs. What could move it, and what each costs:
  - *More development data.* Nothing on this machine has commits that cite decision records beyond the 70 pairs already used. Public repositories with many cited decisions exist; cloning a few needs the network (I read your earlier permission as covering the benchmark's dependencies only, and did not use it for this). With a few hundred development pairs a ranker can be chosen honestly, and Aleph and oxn's ranker can also be compared on pairs neither was shaped on.
  - *A third run on the 100.* Only worth making after a ranker is chosen on such data; it would be reported as the third.
  - *A query that includes the files being changed.* Aleph can do this now (`aleph decisions --for PATH` reads `applies-to`). It is outside oxn's protocol, so it would be a different measure, not this one.
- **Functions inside functions: answered in the code graph now, not shown by `symbol` or `outline`.** Done without asking, as said here before: a call to a function defined inside its caller is answered and certain (eslint's confident recall 48.5% to 81.3%; every confident inner answer right on the six corpora; ADR-003, "Inner functions"). On the held-out Flask corpus three answers are graded wrong where the compiler index gives three different functions one name; I read them as right and reported the figure as measured. *Still yours (entry 9):* whether inner functions should also show in `symbol` and `outline`, which changes what those print for every repository, and whether lambdas with no name should be entities at all.
- **A one-file edit on a large repository takes longer to show than the bar says.** 113 ms from a cold process on oxn's own repository (oxn: 140), 18.5 ms warm (oxn: 62). On Django's 6,833 files: 227 ms cold, 120 ms warm; with `git config core.untrackedCache true` 177 and 70. The rest is git telling what changed. Aleph does not change your git configuration; turning the untracked cache on in a large repository is yours to do (`aleph doctor` says whether it is on).

Also from M8:

- **The tools are off for the agent** unless `ALEPH_GRAPH=1`, like M7's and M9's: `facts` and `decisions`.
- **Routes are not joined to mounts.** `GET /` in a router's file and `/workspace` where that router is mounted are two facts. Joining them across files needs the code graph to follow a router passed as an argument; not built.
- **The facts were graded on two repositories, both yours, and the precision samples were read by me.** A third repository in another framework (Django, Spring, gin) would say more than another sample of these two.
- **A flaky test turned out to be a small bug, fixed.** On a busy machine the embedding worker was stopped for its memory before it was ready, and the next request then said "it did not become ready in time" instead of why. It now says why.
- **Strings in the history of this branch.** The plan said nothing from your repositories would be copied here. The independent review found four test fixture strings and one path in the plan that resemble names in them (an environment name, a task name, a mount path, a file path). They are renamed, but the earlier commits of this branch still hold them. Nothing is pushed: if you want them gone from the history, say so before the first push and I will rewrite those commits.
- **Minor findings of the review, not fixed:** the `oxn.yaml` reader misreads a quoted pattern containing a comma, a mapping nested under a layer, and an apostrophe before a comment (none in the forms oxn writes; it should also say how many lines it skipped); a name both set and read on one line is reported once, as set; a reader that fails on a file records that file as stating nothing until the readers' version changes (the code graph does the same); `aleph decisions --for` a path outside the worktree answers "in no layer" instead of saying so; a `tsconfig.json` you create while the JavaScript indexer runs would be removed with the one it inferred; the first path question of a process misses a path whose word ends in a Greek capital sigma.

## 13. Releasing (M10): what only you decide

M10 makes the repository releasable; it publishes nothing. Before a release:

- **The version, the name it is published under, and where.** It is `0.0.1`, `aleph-zero`, and nowhere. I have not looked the name up anywhere (that needs the network).
- **Whether the gated tools ship on or off:** `references`, `decisions`, `facts` and the compiler indexes are offered to the agent only with `ALEPH_GRAPH=1`, because ADR-003 ships a graph feature only if the A/B run improves with it, and that run spends your plan's usage.
- **More languages.** Python, TypeScript, JavaScript, Go, Rust, Java, C++ and C are read. A further one is yours to name; I have not picked one.
- **What a public tree may say.** Tracked documents name your repositories private repository A and private repository B (you named them; this file, ADR-003, a commit subject) and give counts from them, no names of routes or settings. The plans and ledgers under `docs/superpowers/` describe how this was built, rulings and mistakes included. Both are yours to keep or cut before publishing; and see entry 12 about four strings in earlier commits.
- **Nothing has ever been pushed.** `master` is far ahead of `origin`; the first push is yours.
- **The vector scan's limit is about 2.4 million vectors** (ADR-005, "Where that stops"), sixteen times the largest index on this machine, so no approximate scan was built. A repository beyond it would want one (or 32-bit row numbers, which would move the memory line to about four million): say so if you have such a repository.
