# Deferred work and things waiting for a decision

Things that are not being done now on purpose. Each entry says what it is, why it waits, and how to start it. Newest decisions first. Remove an entry when it is done.

## 1. The long GPU run for Benchmark C (decides whether semantic search is on by default)

- **What:** embed the large Loc-Bench repositories so the Python benchmark has about 400 questions instead of 235. This decides Python only (see 1b for the other languages). The gate is "search with vectors beats search by words and names by at least 5 points of file recall@10, with an interval that excludes zero" (M2 plan, Task 8). With 235 questions the interval is roughly ±6 points; with 400, roughly ±4.5.
- **Why it waits:** it keeps the GPU busy for a long time. The first estimate was about a day. That was made while the worker was degrading with uptime and before data files were excluded from embedding; with both fixed it is probably 6 to 12 hours, but that is a guess until one large repository has been timed. It also fetches these public repositories from GitHub (several GB): vllm, pandas, scikit-learn, transformers, ray, scipy, keras, sympy, qiskit, ultralytics, matplotlib, numpy, mlflow, phidata, streamlit, pytorch-lightning, checkov, pypdf.
- **Now optional.** The 235-question result is in (ADR-002): +5.8 points on issue titles, interval +1.5 to +10.2. That passes the rule fixed beforehand, so Python's verdict is on. The long run would only narrow the interval (to roughly ±3.5); run it if you want more certainty than "passes, thinly".
- **How to start it** (it can be interrupted and resumed; run it when the machine is otherwise idle, and do not run the test suite at the same time):

      uv run --group bench python bench/eval/benchmark_c.py prepare bench/data/benchc --repos vllm-project/vllm,pandas-dev/pandas,scikit-learn/scikit-learn,huggingface/transformers,ray-project/ray,scipy/scipy,keras-team/keras,sympy/sympy,Qiskit/qiskit,ultralytics/ultralytics,matplotlib/matplotlib,numpy/numpy,mlflow/mlflow,phidatahq/phidata,streamlit/streamlit,Lightning-AI/pytorch-lightning,bridgecrewio/checkov,py-pdf/pypdf

      uv run --group bench python bench/eval/benchmark_c.py run bench/data/benchc --out bench/data/results/benchmark_c.json

  A commit with more than 250,000 chunks is skipped (`--max-chunks`); the last line of `prepare` says how many were skipped or left partly embedded.
- **Status:** not started; no longer blocking anything. Waiting for the owner's go-ahead if wanted.

## 1b. Benchmarks for the languages other than Python

- **What:** the gate is read per language (ADR-002), and Loc-Bench is Python only. For C++, C, Go, Java, JavaScript, TypeScript and Rust there are no questions yet, so those languages have no verdict and search by meaning stays off for projects written mainly in them.
- **Order:** by the projects Aleph is activated on. Today that is Python (aleph-zero, oxn, private repository C, and 63% of aeon), then C++ (35% of aeon). The others wait until a project in that language is activated.
- **Question sets found, not fetched:** SWE-bench Multilingual (`SWE-bench/SWE-bench_Multilingual` on Hugging Face: 300 questions, about 12 to 38 per language) and Multi-SWE-bench (`ByteDance-Seed/Multi-SWE-bench`: more repositories per language, among them fmt, nlohmann/json, simdjson, Catch2 and cpp-httplib for C++). Thirty-five questions per language cannot resolve 5 points, so both sets are needed per language.
- **What it costs:** the benchmark harness needs an adapter for each set (the gold files are the files in the fix's patch); then, per language, fetching five to ten public repositories and some hours of GPU embedding.
- **Status:** not started. C++ is next after the Python verdict, with the owner's go-ahead, since it is another GPU run.

## 1c. Tuning runs that need the GPU for hours

- **What:** two things the M2 plan lists and that were not run. (a) The split-threshold sweep of ADR-001: chunk at 96, 128, 192, 256 and 384 tokens and compare on Loc-Bench, with a signature-only control. (b) Re-tuning M1's starting values against Benchmark C: the column weights of the text index, the path and name rules, how many words a long query keeps.
- **Why it waits:** every setting of (a) changes the chunks, so all 235 base commits must be re-indexed and re-embedded per setting: about 1.5 hours of GPU each, five settings. (b) needs no GPU for most settings but a re-index for the column weights.
- **Status:** not started. Ranking and chunking run on the starting values, which passed the gate as they are.

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
- **Why it waits:** nothing is committed in the Aeon repository without the owner's go-ahead. Needed when M6 (the memory layer) starts.

## 6. An upper bound on tree-sitter in oxn

- **What:** oxn requires `tree-sitter>=0.25` with no upper bound; 0.26.0 crashes on large trees in a way Aleph pins against (ADR-007). oxn 0.1.2 was not observed to crash.
- **Why it waits:** the oxn checkout is not to be modified from here.

## 7. Carried to later milestones (recorded in the design)

- M3: C prototypes behind an unknown macro in `.c` files; methods of object literals and chained-assignment lambdas as symbols; a trigram index for Chinese, Japanese and Korean text; file names that are not valid UTF-8 (test on Linux); reclaiming space in the vector file; a Linux file-system check, if it passes the "change, then ask at once" test.
- M4: the Claude Code plugin manifest (with the mod); marking a hit whose path is not on disk; the 2026 MCP handshake.
- M2, still open: whether one query prefix serves both code and documentation (needs questions whose answers are documentation).
