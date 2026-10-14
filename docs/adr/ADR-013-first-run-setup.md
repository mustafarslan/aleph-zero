# ADR-013: Setting Aleph up the first time

Status: built 2026-10-13 (`aleph setup`); tried in a pseudo-terminal on a scratch repository, not yet by the owner

## Question

The owner's words: "how we will setup aleph zero when it first runs? i think it should give us a menu like thing that whatever we need to setup first (for example i might give it repository address of documentation so that it can start embedding it) and other things too then once we have initial setup we would not need to do it again."

Today search by words and names needs no setup at all, and everything else is a command one has to know of: `aleph embed setup`, `aleph embed run`, `aleph source add`, the line that connects Claude Code. Nothing remembers that a person was asked and said no, and the only settings are environment variables.

## Decision

**One checklist, made in one place, and one command where a person answers it.** `aleph setup` shows what can be set up, each line with its state (done, open, declined), what a yes costs and the command that does it, and asks about the open ones. Answers are kept, so nothing is asked twice. The checklist is never put between a person and a search.

- **The plan is a function** (`firstrun.plan`): from what is found on the machine and in the repository and from the answers kept, the list of steps. `aleph setup` asks about it; `aleph doctor` and the agent's `status` tool show it and act on nothing.
- **A search is never interrupted.** The first time `aleph` is used in a repository on a terminal, one line on standard error names `aleph setup`, once. Never from `aleph serve`: its standard output is the protocol, and its standard error is Claude Code's log.
- **Two scopes.** What belongs to the machine (the embedding worker and its model) is answered once for all repositories and kept in the user's cache directory, beside the worker. What belongs to a repository (search by meaning there, linked sources, git's untracked cache) is kept beside its index, as the list of sources already is. Neither is kept in the index, which is a cache and is rebuilt.
- **A yes is for one thing, with its cost said.** Nothing uses the network or the GPU on a general yes. Run without a terminal (an agent, a script), `aleph setup` only shows the list; each thing has a flag of its own (`--worker`, `--embed`, `--source LOCATION`, `--untracked-cache`, `--dense` or `--no-dense` for the repository's choice), and there is no flag that says yes to everything.

### The checklist, in order

1. **Claude Code.** Whether `claude` is found, and the one command that connects it (`claude --plugin-dir …` from this checkout, or `claude mcp add aleph-zero -- aleph serve`). Printed, never run: Aleph does not change Claude Code's settings. The person says when it is done.
2. **The embedding worker and its model** (machine; 310 MB and a build of some minutes; the network). Suggested only where it will be used: when the benchmark's verdict for the repository's main language is in favour of vectors (ADR-002), or the person has asked for search by meaning there.
3. **Linked sources.** A path or a URL of documentation or of another repository, searched beside the code (the owner's example). Not suggested: asked, and skipped with an empty line. Asked before vectors, since what is linked is embedded with the rest.
4. **Search by meaning in this repository.** Makes vectors for its chunks, linked sources included, in the foreground with progress; it can be interrupted and `aleph embed run` continues it, since a chunk that has a vector is not embedded again. Suggested by the same verdict. Where the verdict is against (Python today), a yes also records the choice for the repository, so that the vectors made are used: until now only `--dense` or `ALEPH_DENSE=1` could say that, for one process.
5. **Git's untracked cache.** Only in the form that changes no configuration (`git update-index --untracked-cache`); the `git config` form is printed for those who prefer it. Not suggested.

Not in the first version: compiler indexes (each needs an indexer installed and memory to run it; `aleph scip` and `aleph doctor` cover them), memory across sessions, the steering rung, and the same checklist in Claude Code's pane.

### A setting the engine reads

Search by meaning can now be chosen per repository and kept (`settings.json` beside the index, `"dense": true` or `false`). A flag or `ALEPH_DENSE` still decides for one process; without them the repository's setting decides; without that, the verdict for its main language, as before.

## Three voices

*agy:* the plan as a function with `aleph setup` as its one interactive reader, never a question during a query; its order put git's untracked cache first and ticked (`git config`), the worker ticked whenever a compiler is present, long embedding handed to a background daemon, and Claude Code left out as noise.

*The advisor:* the same shape; and against four points of agy's list, each for a rule or a measurement of this project: Aleph does not change git configuration, so only the index-only form may be run, last and unticked; the worker is suggested by the measured verdict, not by what can be built (a Python repository is not offered 310 MB for nothing); no daemon, since embedding already continues where it stopped; Claude Code is in, since connecting an agent is what the product is for. It added that `--yes` must not spend and that the hint is for a terminal only.

*Mine:* as the advisor. One thing the owner's example shows: in a repository whose language has a verdict against vectors, a documentation source would be searched by words only, so a yes to search by meaning has to be remembered for the repository. Whether vectors help on questions about documentation is not measured; the verdicts are about locating code.

## Accepted when

A fresh repository of Python files, no worker installed: `aleph setup` without a terminal shows five lines, none done except what is found to be so, and changes nothing. With a terminal: a documentation directory given as a source is then found by `aleph search`; a no to the worker is kept; `aleph setup` again asks nothing; `aleph setup --again` asks about what was declined. `aleph doctor` and `status` show the same list. The hint appears once, on a terminal, and never from `aleph serve`.

## As built

- `firstrun.plan` (the list), `settings` (the kept answers), `aleph setup` (the menu and the flags), a row in `aleph doctor`, `setup` in the status the agent's tool returns, and the hint after the commands a person runs (`cli.HINTED`).
- The acceptance above was run through a real pseudo-terminal on a scratch repository with a cache directory of its own: Claude Code answered yes, the worker no, a documentation repository linked by path, git's cache yes. `aleph search` then found the documentation under `handbook:`, the machine's file held the two machine answers, `git config --get core.untrackedCache` was still empty, and a second `aleph setup` asked nothing. Search by meaning was not asked about, since the worker had been declined; the list says so first in that line.
- Differences from the decision: linked sources are asked for before vectors (the decision listed them after), so that what is linked is embedded with the rest. There is no `--yes`: every thing has its own flag.
- Not covered by a test that uses the network: `--worker` and a URL source go through the same code as `aleph embed setup` and `aleph source add`, which have their own tests.

## The review of the branch (a fresh reviewer, 2026-10-13), and what was changed for it

Nothing critical. Four findings graded important, all fixed with a test that failed first:

- **The status could fail where a search works.** Making the list asked whether the worker is installed, and asking created the worker's directory; with a cache directory that cannot be written, `status` raised. Asking now only looks (`worker.where`), and the list's part of the status never raises: it says that the list could not be made.
- **One failing step ended the menu.** A worker that does not build, or a git index that is locked, reached the top of the command; the questions after it and the closing list were lost. A yes that fails is now said and left open, and the menu goes on. A flag that fails does not stop the flags after it; the command then ends with 2.
- **The hint came after a setup made by flag.** Whoever sets anything up, by menu or by flag, is not told of the command afterwards.
- **`--json` with a flag printed a sentence before the data.** What was done is now said on standard error.

Graded minor by the reviewer and fixed in the same pass, because each says or does something untrue to a person:

- Claude Code's own default connects one project, while the answer is kept for the machine: the command printed is now the one for every project (`--scope user`), and the plugin directory is named as what it is, for one session.
- `--embed` without the worker kept a choice and promised that `aleph embed run` would continue: it now does nothing, says to install the worker first, and ends with 2. `--embed` with `--no-dense` is refused.
- A no to vectors where the verdict is already against is kept as an answer, not as the repository's choice: a later verdict in favour is not outranked by a bare return. A no against a verdict in favour is the repository's choice, as before.
- Vectors are done once chosen and begun: an edit, which adds chunks without vectors, no longer makes them a question again; an empty repository has nothing to ask.
- A value for the choice that is neither true nor false is no choice, for the list as for the engine.
- Git's cache is a worktree's, and is called so. The worker's line no longer says "nothing needs it" where vectors are wanted.
- In a directory that is not a repository, the two flags that need one say so in a sentence, and the doctor shows the list there too.
- Two writers of one file (a menu being answered, a first search in another terminal) take turns.

Five tests the reviewer found would pass with the behaviour broken were made to bite. What it checked and found sound: the engine sees a choice made, changed or removed while it runs; a damaged file or one of the wrong shape is no answers; the list costs 0.07 ms of a status; `aleph serve` never reaches the hint; git's configuration is untouched.

Left as it is: whether Claude Code is connected is not detected (it would mean reading its configuration or starting every server it knows); the person says so.

Two things added after the review, before the owner tries it (the advisor's): the command printed for Claude Code names the place this engine is in, since from a checkout `aleph` is on the path only under `uv run` and Claude Code would not find it by name; and the owner's own example is a test from end to end: a documentation repository linked in the menu, a yes to search by meaning, every chunk of the view with a vector, the linked ones among them, and a question in plain words answered with vectors and the documentation among the hits.

## Git's untracked cache is suggested from 3,000 files (the owner's yes, 2026-10-14)

Step 5 was never suggested. It is now pre-ticked where the worktree has 3,000 files or more of its own (`firstrun.UNTRACKED_CACHE_FROM`) and the cache is not on; it is still a step a person answers, still the form that changes no configuration. Measured: a question's look at the worktree 36 → 13 ms on vscode (5,000 files), 288 → 60 on babel (27,000); 7.2 → 7.3 on this repository (235), which is why there is a floor.

- *agy (boost), asked before it was built:* (1) the count was of every file in the view, linked sources included, so a 50-file repository with a 5,000-file source linked would have been pre-ticked: **fixed**, the sources' files are taken off (a test). (2) A no is kept for the repository, while the cache is per worktree's index: kept as it is, because a no in one worktree is the person's answer for the repository, and the step stays in the list to be ticked by hand. (3) `aleph setup` looks at the worktree again before every step: setup only, once, not a question's cost; not changed. (4) A locked index: already said and left open, as the review above made it.
- The commands for the owner's other repositories are given, not run: the engine changes no repository it was not asked to.
