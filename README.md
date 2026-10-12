# aleph-zero

Local code search for coding agents. Results always reflect the working tree,
including uncommitted edits.

## Install

Aleph needs Python 3.13, git, and [uv](https://docs.astral.sh/uv/). From a checkout of this
repository:

    uv sync                                   # a development environment: use `uv run aleph ...`
    uv tool install .                         # or: put `aleph` on your PATH

What it fetches, and when:

- **Grammars** for the languages it parses are downloaded the first time a file of that
  language is read, into your cache directory. `aleph doctor --fetch-grammars` gets them
  all at once; `aleph doctor` says which are missing. Without a language's grammar its
  files are still searched as text, but `symbol` and `outline` find nothing in them.
- **Search by meaning is optional** and off until you run `aleph embed setup`, which builds
  a small local server and downloads a 310 MB model. Without it, search is by words and names.
- Installing fetches Aleph's dependencies, as for any package (and a Python 3.13 if uv has none). After that, Aleph
  uses the network only for the two things above and for a repository you ask it to fetch
  (`aleph source add <URL>`). Nothing about your code ever leaves the machine.

The Claude Code plugin (`plugin/`) is part of the repository, not of the installed package:
point Claude Code at a checkout (see below).

## Quick start

    uv run aleph doctor                       # is everything in place?
    uv run aleph index                        # optional: the first search indexes too
    uv run aleph search "where are refresh tokens rotated"
    uv run aleph search TokenStore.get --detail full
    uv run aleph symbol refresh_token         # where a name is defined
    uv run aleph outline src/auth/store.py    # what a file defines, with line ranges
    uv run aleph status

Useful options for `search`: `--kind code|docs`, `--glob "src/api/*"`, `--lang python`,
`--detail locations|snippets|full`, `--json`, `--timeout-ms 500`.

## Use it from Claude Code

    claude mcp add aleph-zero -- /absolute/path/to/aleph-zero/.venv/bin/aleph serve
    claude mcp add aleph-zero -- aleph serve          # if you installed it with `uv tool install`

The server offers four tools for finding code. `search` is always loaded; `symbol`, `outline`
and `status` load on demand. Each takes `cwd`, so one server answers for any worktree. Two
more, `recall` and `remember`, belong to the memory layer below.

### As a plugin, with steering

    claude --plugin-dir /absolute/path/to/aleph-zero/plugin

The plugin starts the same server and adds a mod that steers Claude Code towards it.
How far it goes is one option, the rung (`/config`, or `pluginConfigs` in settings):

| Rung | What it adds |
|---|---|
| 0 | Nothing: the tools as above |
| 1 (default) | All four tools loaded up front with tuned descriptions, a short standing instruction to search before recursive grep, and a pointer to it in the descriptions of Claude Code's own Grep and Glob |
| 2 | Ranked locations ride along with the first prompt of a session, when the engine is confident |
| 3 | A recursive `grep`, `rg`, `git grep` or `find -name` that found nothing gets ranked locations appended |

The pointer in Grep and Glob has its own switch (`grep_pointer`: on or off).

`/aleph` with no argument opens a side chat about the repository: questions are answered from
the index with `path:line` citations, by a model and effort you pick in the pane, and nothing
reaches the main conversation unless you press Insert (ADR-012).

Rungs 2 and 3 have the mod itself ask the engine, which Claude Code allows only if the
engine's tools are allowed: add `"mcp__plugin_aleph-zero_aleph__*"` to `permissions.allow`.
`/aleph check` says whether the mod can reach the engine; `/aleph stats` shows the session's
tokens, tool calls and how often search was used before grep (`/aleph stats last` for the
session before). Which rungs pay is being measured (ADR-011); until then the default is 1.

## Documentation that lives in another repository

    uv run aleph source add ../team-docs --name docs        # read in place, follows that clone
    uv run aleph source add https://example.com/handbook.git   # cloned once; uses the network
    uv run aleph source refresh                                # fetch URL sources again
    uv run aleph source list

Its files are searched as `docs:path/in/that/repo.md`.

## Search by meaning

    uv run aleph embed setup      # once: builds the worker and downloads the model (310 MB; uses the network)
    uv run aleph embed run        # gives this worktree's code and docs their vectors (about 90 chunks a second)
    uv run aleph embed status

With the worker installed, vectors are used by default in projects whose main language
has a benchmark verdict in their favour: today that is Python (ADR-002). `aleph status`
shows a project's main language and whether vectors are on, and why. `--dense`,
`--no-dense` and `ALEPH_DENSE=1|0` decide by hand. One worker serves every session and
repository; it unloads its model after ten idle minutes and exits after thirty.

## Memory across sessions

What the side chat answered, and the notes you inserted from it, are kept per repository
and can be recalled later (the `recall` tool, or the command line):

    uv run aleph memory list
    uv run aleph memory recall "why did we change token rotation"
    uv run aleph memory forget <id>        # or --all

A recalled memory says when it was made, what code it cites, and whether that code has
changed since. Without anything else installed, recall is by words and recency. With
Aeon's core extension available (`ALEPH_AEON_LIB=/path/to/its/build`), recall is also by
meaning (ADR-006). The text stays on this machine, in the repository's state directory.

## Faster queries

Every query asks git what the working tree looks like. On a large repository that is
most of a query's time. Git can remember untracked files between calls:

    git config core.untrackedCache true

On Django (7,000 files) this takes a query from about 80 ms to about 30 ms.
`aleph doctor` shows whether it is on. Aleph never changes your git configuration itself.

## Who calls what

    uv run aleph callers TokenStore.get      # every call that reaches it
    uv run aleph callees refresh_token       # what it calls

Each answer says how it was found. A call is listed as certain when the code itself says
where it goes: the same file, an import followed to the file that declares the name, the
class the call sits in or one it is built on, a parameter or field whose type is stated.
A call on a value whose type nothing states is listed apart, as one of the definitions
that share the name. A call into a library is not listed at all. A call to a function
defined inside the calling function is listed, as certain unless the name is given another
value somewhere or defined twice; such a function is not itself found by `aleph symbol`.

The first question about a worktree reads every file's calls and imports (a few seconds
on a large repository); later ones take milliseconds. Searching never pays for this.
The agent is offered the matching `references` tool only when `ALEPH_GRAPH=1` is set
for the server: whether it helps an agent has not been measured yet.

### Exact answers from a compiler's index

    uv run aleph scip import index.scip             # an index made elsewhere (CI, by hand), from HEAD
    uv run aleph scip import index.scip --tree v1.4 # ... or from another revision
    uv run aleph scip import index.scip --worktree  # ... or from the worktree as it is now
    uv run aleph scip run --lang python             # or let Aleph run the indexer here
    uv run aleph scip list

A [SCIP](https://github.com/sourcegraph/scip) index is what a compiler knows about the
code. Once one is imported, `callers` and `callees` answer from it where it has an answer,
marked `(compiler index)`: also for calls on values whose type nothing in the source
states. With uncommitted changes, say what the index was made from (`--tree`, `--worktree`).

It is believed only for files that are still what it was made from, and whose
dependencies are too. Edit a file, and that file and everything that depends on it are
answered by the resolver again until a new index is imported; the answer says so.

`aleph scip run` runs the indexer for the languages where its memory was measured under
the limit (Python, Go, Rust, Java with Maven), watches it, and stops it above 2,048 MB
(`ALEPH_SCIP_MEMORY_MB`). JavaScript and TypeScript need more than that (2.3 GB on eslint)
and run only when you name both the language and a limit above the default:
`ALEPH_SCIP_MEMORY_MB=3072 aleph scip run --lang javascript`. For Java and Rust this compiles the project, in a copy of its
files: your worktree is not touched, and it is only ever done when you ask. Aleph never
installs an indexer; `aleph doctor` says which are present.

## What the repository says about itself

    uv run aleph facts                      # how many routes, models, environment names, tasks ... it states
    uv run aleph facts route --like users   # each with its file and line
    uv run aleph facts env --like REDIS     # where a setting is read, set, or declared in an example file
    uv run aleph decisions "cache the invoice totals in redis"   # the decision records that govern a task
    uv run aleph decisions --for src/billing/invoice.py          # the records that name this file, and its layer

`aleph facts` reads what a file says in so many words: a route registered under a literal
path (Express, FastAPI, Flask, Go's net/http, chi, gin, Spring), where a router is mounted,
a model or a table (SQLAlchemy, Django, Alembic, JPA, Prisma), an environment name, a
Celery task, a queue, a gRPC service. A route's path is as written in its file; a mount's
prefix is listed as its own fact and not added to it. The first question about a worktree
reads every file once (2.5 s on a repository of 1,300 files); searching never pays for it.
A call like `x.get("/users", handler)` is read as a route only when `x` is known for a router
(the file makes it one, or it is called `app`, `router`, `apiRouter` and the like): a lookup
reported as an endpoint would be the worse mistake. Express, FastAPI, Prisma, Alembic, Celery,
Go's net/http and gRPC were measured on real repositories; the other readers are covered by
tests only (ADR-003).

`aleph decisions` ranks decision records (numbered Markdown or reStructuredText files in a
directory such as `docs/adr` or `docs/decisions`) as whole documents for a task, and answers in a few hundred tokens. When the
embedding worker is running, each record's own vector is added to the words (a record no
word of the task names can then still be found); without it the ranking is by words. With
`--for`, it lists the records whose `applies-to` frontmatter covers a file and, when the
repository has an [oxn](docs/adr/ADR-007-relationship-to-oxn.md) `oxn.yaml`, the layer the
file is in and what that layer may depend on. Aleph reads these; it enforces nothing.

Like `references`, the matching tools are offered to the agent only with `ALEPH_GRAPH=1`.

## Without git

Aleph also searches a directory that is not inside a git repository: the files as they are
on disk, this directory and below (in a repository it is always the whole repository).
Nothing says what belongs to such a directory, so some things are left out: every file and
directory whose name begins with a dot, `node_modules`, `__pycache__`, `venv`, directories
marked with a `CACHEDIR.TAG`, symbolic links, and git repositories inside it (run Aleph in
those). `aleph status` and `aleph doctor` say so.

It refuses, with one sentence, where walking would be a mistake: your home directory or
anything that holds it (however the path is spelled), the top of a disk, system
directories (`/usr/local`, `/var`, somebody else's home), the temporary directories
themselves, and a directory of more than 20,000 files (or one where more than
200,000 entries would have to be looked at). For those, `git init` and a `.gitignore` are
the way to say what belongs to the project. A directory with a `.git` at or above it that
git cannot read is an error, not a plain directory: what it ignores must stay ignored.

So `.github/` and other hidden files are not searched here, files over 300 KB and binary
files are left out as everywhere, and the first search of a large directory reads all of
it once (about five seconds for 5,000 files); later ones read only what changed.

## Where things are

The index lives in `<git-common-dir>/aleph-zero/index.db` and is shared by every
worktree of the repository. It is a cache: deleting it is always safe. When git's
directory cannot be written, or is on a network filesystem, it lives in
`~/.cache/aleph-zero/index/` instead; `aleph status` says where and why.

In a directory that is not a git repository the index always lives in `~/.cache/aleph-zero/index/`:
Aleph writes nothing into such a directory.

What is searched is what git tracks in this worktree, as it is on disk now:

- In a sparse checkout, files outside it are not searched. `status` says how many.
- Submodules are not searched. `status` and `doctor` list them; link one as a source
  with `aleph source add path/to/submodule`.
- Shallow and partial clones work. Nothing is ever fetched to answer a query.
- A directory that is not a git repository is searched as described under "Without git", with what that leaves out.

Content no worktree has used for 30 days is dropped by itself; `aleph gc --days N`
does it on request. A damaged index file is set aside (kept as `index.db.damaged-…`)
and built again.
Supported languages: Python, TypeScript, JavaScript, Go, Rust, Java, C++, C, plus
Markdown and reStructuredText. Other text files are searched as plain text.

Design: `docs/superpowers/specs/`. Decisions and measurements: `docs/adr/`.
