# Deferred work and things waiting for a decision

Things that are not being done now on purpose. Each entry says what it is, why it waits, and how to start it. Newest decisions first. Remove an entry when it is done.

## 1. The long GPU run for Benchmark C (decides whether semantic search is on by default)

- **What:** embed the large Loc-Bench repositories so the benchmark has about 400 questions instead of 235. The gate is "search with vectors beats search by words and names by at least 5 points of file recall@10, with an interval that excludes zero" (M2 plan, Task 8). With 235 questions the interval is roughly ±6 points; with 400, roughly ±4.5.
- **Why it waits:** it keeps the GPU busy for a long time. The first estimate was about a day. That was made while the worker was degrading with uptime and before data files were excluded from embedding; with both fixed it is probably 6 to 12 hours, but that is a guess until one large repository has been timed. It also fetches these public repositories from GitHub (several GB): vllm, pandas, scikit-learn, transformers, ray, scipy, keras, sympy, qiskit, ultralytics, matplotlib, numpy, mlflow, phidata, streamlit, pytorch-lightning, checkov, pypdf.
- **Decide first:** read the 235-question result (`bench/data/results/benchmark_c.json`, summarised in ADR-002 when it is in). If the gain on the title form is clearly above 5 points or clearly near zero, the long run may not be needed.
- **How to start it** (it can be interrupted and resumed; run it when the machine is otherwise idle, and do not run the test suite at the same time):

      uv run --group bench python bench/eval/benchmark_c.py prepare bench/data/benchc --repos vllm-project/vllm,pandas-dev/pandas,scikit-learn/scikit-learn,huggingface/transformers,ray-project/ray,scipy/scipy,keras-team/keras,sympy/sympy,Qiskit/qiskit,ultralytics/ultralytics,matplotlib/matplotlib,numpy/numpy,mlflow/mlflow,phidatahq/phidata,streamlit/streamlit,Lightning-AI/pytorch-lightning,bridgecrewio/checkov,py-pdf/pypdf

      uv run --group bench python bench/eval/benchmark_c.py run bench/data/benchc --out bench/data/results/benchmark_c.json

  A commit with more than 250,000 chunks is skipped (`--max-chunks`); the last line of `prepare` says how many were skipped or left partly embedded.
- **Status:** not started. Waiting for the owner's go-ahead.

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
- **To add before filing:** the second finding from M2, if the measurement under way confirms it: with the default prompt cache, an embeddings server slows down and grows with every request (after 4.5 hours: a tenth of the speed and 1,008 MB).

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
