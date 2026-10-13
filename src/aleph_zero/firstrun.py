"""Setting Aleph up the first time (ADR-013): what can be set up, as a list made in one place.

`plan` is a function of what is found on the machine and in the repository and of the answers kept:
`aleph setup` asks about the list, `aleph doctor` and the agent's status tool show it. Nothing here does
anything; the commands that do are named in each step.
"""
from __future__ import annotations

from dataclasses import dataclass

CHUNKS_A_SECOND = 100          # what the worker embeds on this kind of machine (ADR-005): for the estimate only
WORKER_COST = "310 MB download and a build of a few minutes; uses the network"


@dataclass(frozen=True)
class Found:
    """What was found, before anything is asked."""
    claude: bool               # `claude` is on PATH
    plugin_dir: str | None     # the plugin directory of the checkout this engine runs from, if it runs from one
    worker: bool               # the embedding worker and its model are installed
    language: str | None       # the repository's main language
    verdict: bool              # the benchmark's verdict for that language is in favour of vectors
    verdict_why: str
    chunks: int                # chunks of the worktree, linked sources included
    with_vector: int
    sources: int               # linked sources
    untracked_cache: bool      # git keeps its untracked cache for this repository
    repository: bool           # a git repository: sources are linked to one, and git's cache is its own


@dataclass(frozen=True)
class Step:
    key: str
    scope: str                 # 'machine': answered once for every repository; 'repository'
    title: str
    state: str                 # 'done', 'open' or 'declined'
    detail: str                # what is known now
    cost: str                  # what a yes costs; empty when it costs nothing
    command: str               # what does it
    suggested: bool            # offered with yes as the answer to a bare return


def _state(done: bool, answers: dict, key: str) -> str:
    kept = answers.get("setup", {}).get(key) if isinstance(answers.get("setup"), dict) else None
    return "done" if done or kept == "done" else "declined" if kept == "declined" else "open"


def plan(found: Found, repo: dict, machine: dict) -> list[Step]:
    """The checklist, in the order it is asked. `repo` and `machine` are the kept answers (settings.read)."""
    asked_for = repo.get("dense")                      # the person's own choice for this repository, if they made one
    wanted = found.verdict if asked_for is None else bool(asked_for)
    connect = " or ".join(filter(None, [f"`claude --plugin-dir {found.plugin_dir}`" if found.plugin_dir else "",
                                        "`claude mcp add aleph-zero -- aleph serve`"]))
    steps = [
        Step("claude", "machine", "Connect Claude Code", _state(False, machine, "claude"),
             "`claude` is on this machine" if found.claude else "`claude` was not found on PATH",
             "", connect, found.claude),
        Step("worker", "machine", "The embedding worker and its model (search by meaning)", _state(found.worker, machine, "worker"),
             "installed" if found.worker else "not installed; nothing needs it while search is by words and names",
             "" if found.worker else WORKER_COST, "aleph embed setup", wanted),
    ]
    if found.repository:
        steps.append(Step(
            "sources", "repository", "Documentation or code in another repository, searched beside this one",
            _state(found.sources > 0, repo, "sources"),
            f"{found.sources} linked" if found.sources else "none linked",
            "a path is read where it is; a URL is cloned into the cache directory (uses the network)",
            "aleph source add <path or URL>", False))
    missing = max(found.chunks - found.with_vector, 0)
    made = found.chunks > 0 and missing == 0
    minutes = max(1, round(missing / CHUNKS_A_SECOND / 60))
    no_worker = not found.worker and steps[1].state == "declined"      # nothing to make vectors with, and none wanted
    vectors = "declined" if asked_for is False or no_worker else _state(made and wanted and found.worker, repo, "vectors")
    steps.append(Step(
        "vectors", "repository", "Search by meaning in this repository", vectors,
        f"{found.verdict_why}; {found.with_vector:,} of {found.chunks:,} chunks have a vector"
        + ("" if asked_for is None else f"; this repository is set to {'use' if asked_for else 'not use'} vectors")
        + ("; it needs the embedding worker, which was declined" if no_worker else ""),
        "" if made else f"about {minutes} min of this machine's GPU for {missing:,} chunks; it can be interrupted and continued",
        "aleph embed run", wanted))
    if found.repository:
        steps.append(Step(
            "untracked_cache", "repository", "Git's untracked cache (a faster look at the worktree on every query)",
            _state(found.untracked_cache, repo, "untracked_cache"),
            "on" if found.untracked_cache else "off",
            "", "git update-index --untracked-cache", False))
    return steps


def open_steps(steps: list[Step]) -> list[str]:
    return [step.key for step in steps if step.state == "open"]
