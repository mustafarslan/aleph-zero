# ADR-014: One command that brings a repository up to date

Status: built 2026-10-13 (`aleph update`); tried with real git hooks on a scratch repository

## Question

The owner asked how the embeddings, the knowledge graph and the rest stay current when a project changes with every merged pull request, without everything being made again each day. The answer is that nothing is made for a project as a whole, so nothing is made again as a whole: the index, the vectors, the code graph's facts and the architecture facts are each kept per content, and only what changed is read (shown on a scratch repository: a commit that changes one function and adds one file indexes 2 files of 40 and stores 2 chunks, in 0.11 s; the graph's facts go from 42 rows to 43, the 42 untouched). But it all happens when the next question is asked, and two things do not happen by themselves at all: a source linked by URL is fetched only when asked, and vectors are only made while a server runs or `aleph embed run` is called. The owner: "aleph update could work we can do it too".

## Decision

`aleph update` brings a repository up to date in one go, for a person, a git hook or a nightly schedule. It does what the engine would do at the next question and the two things nothing does by itself, in this order, since each feeds the next:

1. **Sources linked by URL are fetched** (`--no-fetch` leaves the network alone). A fetch that fails keeps the last good state and says how old it is; it is a line of the report, not a failure of the command.
2. **The worktree is indexed**, everything pending.
3. **The code graph's and the architecture's facts are read for what changed, but only where they were ever asked for.** An index that is only searched pays nothing for them (the schema's own rule), and a hook does not quietly change that.
4. **Vectors are made for chunks without one, but only where search by meaning is on already**: the language's verdict or the choice kept by `aleph setup`. The command never switches it on; where it is off, the line says why. Bounded by `--minutes` (10 unless given): what is left is said and is made next time. `aleph embed run` stays the unbounded one.
5. **Compiler indexes that are out of date are counted and named, never run**: they are external tools over a whole project, with their own memory limits, and stay on request (ADR-004).

Garbage is collected on the engine's own daily schedule and is not part of this.

**For a hook.** `--print-hook` prints the lines for git's `post-merge` and `post-checkout` hooks; Aleph installs no hook and changes no git configuration. The printed lines name the place this engine is in, run the update detached and silent so that `git pull` and `git checkout` return at once, bound the embedding to a few minutes, and for `post-checkout` act only on a change of branch (git calls that hook for a single restored file too).

**Unattended, so:** one update at a time for a repository (a second one says that another is running and ends without an error: rapid checkouts start several); "another session is embedding this repository" is a line, not an error; the one line that names `aleph setup` is never said by this command; `--quiet` prints only what failed, `--json` gives the report as data; the command ends with 0 unless a part failed.

**And one fix that a hook makes necessary.** Git runs its hooks with `GIT_DIR`, `GIT_INDEX_FILE` and their like set to the repository the hook belongs to. Aleph runs git for other repositories too (a source's clone in the cache directory), and passed its environment on: from inside a hook those commands would have acted on the wrong repository. Aleph's git commands now run without those variables: the directory it names with `-C` is the repository it means.

## Three voices

*The advisor:* the order (sources, index, facts, vectors); never force vectors (`aleph embed run` does, this must not, or a nightly hook switches them on in every Python repository against the verdict); facts only where they are in use; compiler indexes reported, not run; hooks printed, never installed, with the engine's path and a bound on the minutes; no setup hint from this command; garbage left to its schedule.

*agy:* four things for a command run from a hook: git's own variables in a hook's environment, which Aleph passed on to its git commands (checked in `gitio._env`: true); a hook runs in the foreground and holds `git pull` unless the printed line detaches it; no lock around the whole command, so rapid checkouts overlap; `post-checkout` is also called for a single file and must look at its third argument. All four are adopted.

*Mine:* as both. The default bound of ten minutes is mine: enough for a day's merges at a hundred chunks a second (60,000 chunks), short enough that a forgotten terminal is not held for an hour.

## Accepted when

On a repository with a commit just made: `aleph update` indexes what changed and says so in a line; run again it says that nothing changed. Where vectors are on, every chunk has one afterwards, and where they are off none is made and the worker is not asked. A source linked by URL whose origin moved is fetched and its new content found, and with `--no-fetch` it is not. Graph facts are read where they were in use and not created where they were not. Two updates at once: one works, the other says so and ends with 0. With `GIT_DIR` set to another repository, Aleph still reads the one it was pointed at. `--print-hook` prints and writes nothing.

## As built

- `update.run` (the parts, in order, each failing by itself), `update.only_one` (the lock), `update.hook_lines`, `aleph update` with `--no-fetch`, `--minutes`, `--quiet`, `--json`, `--print-hook`; `gitio` runs git without the variables a hook sets.
- Tried on a scratch repository with a cache directory of its own: a commit changing one function: "indexed 1 file" and "read the code graph's facts of 1 file" (the graph had been asked there), then "nothing changed". The printed lines put into its hooks by hand: after a change of branch and after a merge the index matched the worktree without any command of mine, and the merged file's definition was in it. With `GIT_DIR` set to another repository a search still answered from the one it was pointed at.
- Not tried with the real worker: it was embedding the TypeScript benchmark; the vectors' part is covered by the tests with the stand-in worker, as `aleph embed run` is.
- `aleph setup` names the command at the end of its menu.
- **One behaviour of ADR-010 changes with this.** There, a `GIT_DIR` left behind in the environment that pointed nowhere made Aleph refuse the directory ("git cannot read the repository"), so as not to search a repository as a plain directory. Now that variable does not reach git at all: Aleph reads the repository it was pointed at. The refusal stays for the cases that are still git's to say: a damaged `.git`, and `GIT_CEILING_DIRECTORIES`. The test that pinned the old behaviour failed in the full suite and was changed with this decision, not around it.

