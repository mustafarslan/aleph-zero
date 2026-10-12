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
- **After M6:** M7 to M10 (code graph, knowledge graph, SCIP, public release) each *ship* only if the A/B measurement improves (entry 0). M7 is being built ahead of that gate: its tools stay on demand and nothing is added to search until the measurement says it pays.

## 6. An upper bound on tree-sitter in oxn

- **What:** oxn requires `tree-sitter>=0.25` with no upper bound; 0.26.0 crashes on large trees in a way Aleph pins against (ADR-007). oxn 0.1.2 was not observed to crash.
- **Why it waits:** the oxn checkout is not to be modified from here.

## 7. Left out of M3 on purpose (ADR-010)

- **Searching a directory that is not a git repository.** Today it gets one clear sentence (`git init`). Doing it properly needs its own way to list, hash and watch files, and a rule against walking a home directory. Planned for M10; say if you want it sooner.
- **Things that need Linux:** file names that are not valid UTF-8 (cannot be created on macOS); reading a session's own memory there is written (`/proc/self/smaps_rollup`) but has only been run on macOS; the network-filesystem check reads `/proc/self/mountinfo` and is likewise untested there; a file-system watcher, if it passes the "change, then ask at once" test.
- **Two installed versions of Aleph with different chunkers sharing one index** re-process each other's content on every switch (vectors survive it since M3). Matters when more than one version is in use: M10.
- **The vector file never shrinks.** Rows of collected content are used again, so it stops growing, but after a very large branch is abandoned the file stays at its high-water mark (150 MB per 300,000 chunks). Deleting `vectors.*.f16` and running `aleph embed run` rebuilds it.
- **A trigram index for text written without spaces** (Chinese, Japanese, Korean): no repository in use needs it.
- **Plain values of object literals** (`limit: 10`) are not listed as symbols; functions in them are.
- **Left as they are after the M3 review, with their cost:** vector rows are flushed with `fsync`, not macOS's stronger `F_FULLFSYNC`, so after a power failure (not a crash) a row could hold older bytes than the index says; a cache directory on a network home directory is not detected; a collection round killed half-way waits a day for the next; while a session's own background thread is embedding, each of its queries re-checks the whole view (about 2 ms on Django).

## 8. Carried to later milestones (recorded in the design)

- M4, still open: the 2026 MCP handshake (the engine still relies on the client's fallback to `initialize`); L4 of the steering ladder, if the measurement leaves a gap.
- M2, still open: whether one query prefix serves both code and documentation (needs questions whose answers are documentation).
