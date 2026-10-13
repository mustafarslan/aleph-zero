# ADR-014: One command that brings a repository up to date

Status: built 2026-10-13 (`aleph update`); tried with real git hooks on a scratch repository

## Question

The owner asked how the embeddings, the knowledge graph and the rest stay current when a project changes with every merged pull request, without everything being made again each day. The answer is that nothing is made for a project as a whole, so nothing is made again as a whole: the index, the vectors, the code graph's facts and the architecture facts are each kept per content, and only what changed is read (shown on a scratch repository: a commit that changes one function and adds one file indexes 2 files of 40 and stores 2 chunks, in 0.11 s; the graph's facts go from 42 rows to 43, the 42 untouched). But it all happens when the next question is asked, and two things do not happen by themselves at all: a source linked by URL is fetched only when asked, and vectors are only made while a server runs or `aleph embed run` is called. The owner: "aleph update could work we can do it too".

## Decision

`aleph update` brings a repository up to date in one go, for a person, a git hook or a nightly schedule. It does what the engine would do at the next question and the two things nothing does by itself, in this order, since each feeds the next:

1. **Sources linked by URL are fetched** (`--no-fetch` leaves the network alone). A fetch that fails keeps the last good state and says how old it is; it is a line of the report, not a failure of the command.
2. **The worktree is indexed**, everything pending.
3. **The code graph's and the architecture's facts are read for what changed, but only where they were ever asked for.** An index that is only searched pays nothing for them (the schema's own rule), and a hook does not quietly change that.
4. **Vectors are made for chunks without one, but only where search by meaning is on already**: the language's verdict or the choice kept by `aleph setup`. The command never switches it on; where it is off, nothing is said of it in the text (a line on every pull would be noise) and `--json` gives the reason. Where every chunk has its vector the worker is not started. Bounded by `--minutes` (10 unless given): what is left is said and is made next time. `aleph embed run` stays the unbounded one.
5. **Compiler indexes that are out of date are counted and named, never run**: they are external tools over a whole project, with their own memory limits, and stay on request (ADR-004).

Garbage is collected on the engine's own daily schedule and is not part of this.

**For a hook.** `--print-hook` prints the lines for git's `post-merge` and `post-checkout` hooks; Aleph installs no hook and changes no git configuration. The printed lines name the place this engine is in (quoted: a path may hold a space), run the update detached and silent so that `git pull` and `git checkout` return at once, leave the network alone (a hook has no business fetching on every change of branch; the nightly line does), bound the embedding to a few minutes, and for `post-checkout` act only on a change of branch, written so that the hook ends well when it is not one: git calls that hook for a single restored file too, and a hook that fails fails the command.

**Unattended, so:** one update at a time for a worktree (a second one says that another is running and ends without an error), and the one at work looks at the worktree again before it ends, up to three times, so that a checkout that lands meanwhile is not lost; another worktree of the same repository is not held up, since the index is made for several sessions at once. "Another session is embedding this repository" is a line, not an error. A fetch nobody watches never asks for a name or a password on the terminal and does not carry the options of the command that started the hook (`git -c ... pull`) to another repository's remote. An index that another session is writing to is a line saying so, not a traceback. The one line that names `aleph setup` is never said by this command; `--quiet` prints only what failed, `--json` gives the report as data with the same keys whatever happened; the command ends with 0 unless a part failed. A fetch that fails is not such a failure: under `--quiet` it is silent, and `aleph source list` and `aleph doctor` show how old a source is.

## Three voices

*The advisor:* the order (sources, index, facts, vectors); never force vectors (`aleph embed run` does, this must not, or a nightly hook switches them on in every Python repository against the verdict); facts only where they are in use; compiler indexes reported, not run; hooks printed, never installed, with the engine's path and a bound on the minutes; no setup hint from this command; garbage left to its schedule.

*agy:* four things for a command run from a hook: git's own variables in a hook's environment; a hook runs in the foreground and holds `git pull` unless the printed line detaches it; no lock around the whole command, so rapid checkouts overlap; `post-checkout` is also called for a single file and must look at its third argument. The last three are adopted. The first was adopted as "run git without `GIT_DIR` and its like" and then withdrawn after the review: see below.

*Mine:* as both. The default bound of ten minutes is mine: enough for a day's merges at a hundred chunks a second (60,000 chunks), short enough that a forgotten terminal is not held for an hour.

## Accepted when

On a repository with a commit just made: `aleph update` indexes what changed and says so in a line; run again it says that nothing changed. Where vectors are on, every chunk has one afterwards, and where they are off none is made and the worker is not asked. A source linked by URL whose origin moved is fetched and its new content found, and with `--no-fetch` it is not. Graph facts are read where they were in use and not created where they were not. Two updates of one worktree at once: one works, the other says so and ends with 0. The printed hook lines, run by `sh`, end with 0 for a restored file and for a change of branch. `--print-hook` prints and writes nothing.

## As built

- `update.run` (the parts, in order, each failing by itself, the worktree looked at again before the end), `update.only_one` (the lock, a worktree's), `update.hook_lines`, `aleph update` with `--no-fetch`, `--minutes`, `--quiet`, `--json`, `--print-hook`; `gitio.unattended` for a fetch nobody watches.
- Tried on a scratch repository with a cache directory of its own: a commit changing one function: "indexed 1 file" and "read the code graph's facts of 1 file" (the graph had been asked there), then "nothing changed". The printed lines put into its hooks by hand: `git checkout -- file` and `git restore file` end with 0, and after a change of branch and after a merge the index matched the worktree without any command of mine, the merged file's definition in it.
- Not tried with the real worker: it was embedding the TypeScript benchmark; the vectors' part is covered by the tests with the stand-in worker, as `aleph embed run` is.
- `aleph setup` names the command at the end of its menu.

## The review of the branch (a fresh reviewer, 2026-10-13), and what was changed for it

Two findings graded critical, both mine to have caught:

- **The printed `post-checkout` line failed git.** It was `[ "$3" = "1" ] && ( … )`: for a single restored file the test is false and is the script's last command, so the hook ended with 1 and `git restore a.py` reported a failure. It is now an `if`, and a test runs the printed lines under `sh`.
- **Running git without `GIT_DIR` and `GIT_WORK_TREE` was wrong, and is withdrawn.** agy had said that git sets these for hooks and that Aleph would pass them on to commands meant for another repository. Looked at instead of believed (git 2.56): the hooks of an ordinary repository carry `GIT_PREFIX`, `GIT_EXEC_PATH` and sometimes `GIT_CONFIG_PARAMETERS` and `GIT_INDEX_FILE`, but no `GIT_DIR`. `GIT_DIR` and `GIT_WORK_TREE` are in the environment when a repository is named by them alone (`git --git-dir=… --work-tree=…`, the usual way to keep dot files), and there they are what makes the directory a repository: without them Aleph took it for a plain directory and indexed what its ignore rules keep out. The environment is passed on as before, and ADR-010's refusal of a `GIT_DIR` that points nowhere stands again. What that left open was checked the same evening and was a real loss of data: with `GIT_DIR` in the environment, the fetch of a linked source ran in the user's own repository, replaced its `main` and deleted its other local branches. The right cut is not "no git command gets these variables" (the first attempt) nor "every one does" (as it was), but by which repository a command is meant for: those for another repository run without them (`gitio.elsewhere`), those for the repository Aleph was pointed at keep them. Found by agy with boost, on Gemini 4 Argon, in a second review of this command; verified on scratch repositories before and after.

Graded important, fixed in the same pass with a test that failed first: an index held by another writer ended the command with a traceback (the store's errors are not the kind that were caught); the hooks fetched linked sources on every pull and change of branch, and a fetch could ask for a password on the terminal; an update that found another one at work was lost, and so were the changes it came for; options given to `git -c … pull` travelled with the fetch of a linked source.

Smaller, fixed: the engine's path was not quoted; the report's keys differed on the busy path; `ALEPH_DENSE` was not honoured. Left as they are, with their cost: where vectors are off the text says nothing of it (the reason is in `--json`: a line on every pull would be noise); the facts count as "in use" when any are stored, so after an index is rebuilt they are read again only once someone asks; a failed fetch is silent under `--quiet`.

Tests the reviewer found would pass with the behaviour broken were replaced or sharpened: the hook lines are run, a failed fetch must say "failed", the worker's every entrance is closed where vectors are off, and a failing part, the bound on minutes, a second worktree and a change during the update each have a test.

## A second review, by agy with boost (2026-10-13), each finding checked before it was believed

The first finding was the loss of data told above. The rest, as they turned out when looked at:

| Reported | Looked at | What was done |
|---|---|---|
| The list of sources is written without a lock, under one scratch name | True: a source linked while a refresh was fetching (up to a minute) was dropped when the refresh wrote the list back as it had read it | One writer at a time (`sources._one_writer`), a scratch file of the writer's own, and a refresh that puts what it learned into the list as it is then, a source at a time. Never held across the network |
| A fetch that runs out of time waits for ever on a helper | Half true: the command did end at its limit, but git's helpers (`git-remote-http`) were left running, a pair for every fetch that timed out | A command out of time takes its children with it (`gitio._stop_with_children`). Not by a process group of its own: that would take git out of the terminal's foreground, and a person linking a source would never be asked for the password git wants |
| An update never runs the daily collection | True, and worse than it sounds: a query collects only after it stored new content, and where a hook runs `aleph update` after every pull a query never does. Nothing would ever have been collected | The update does it after indexing, as a query does |
| The update starts the worker although nothing needs a vector | True: a hook after every pull would have loaded a 300 MB model to be told there is nothing to do | Not started when every chunk has its vector |
| `git rebase` fires the hook for every commit | False for rebase as measured (git 2.56): once, at its start. True for `git bisect`: every step | The printed hooks stay out of both |
| The engine's path comes from the Python it runs under | True for a Python that is an application bundle: nothing is beside it | `firstrun.engine_path`: beside the Python if it is there, else what the shell finds, else the name |
| The hook's command can read the terminal | True | `</dev/null` |
| The source's tree is looked up outside the time limit | True | Inside it, and as unattended as the fetch |
| The test of `--minutes` passes with the bound broken; the architecture's facts and the compiler indexes have no test | True; the three behaviours were right | Tests that bite: 400 functions and a budget of nothing leave chunks without a vector; the facts are read where in use; out-of-date compiler indexes are counted |
| `import fcntl` does not exist on Windows | True and not a finding: the worker, the vector file and the settings are for macOS and Linux already | Nothing |

**And one thing no review reported, found by a test that failed one run in six:** where a repository's state lives is decided by trying to write a probe file beside git's data, and the probe had one name for the whole process. Two threads asking first at the same moment removed each other's file; the second took that for "cannot be written" and put its state into the cache directory. A server has two threads that ask first: the one that answers and the one that embeds. One session could so have had two indexes. The probe is the thread's own now and the decision is made once (`store/db.py`).

