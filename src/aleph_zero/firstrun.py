"""Setting Aleph up the first time (ADR-013): what can be set up, as a list made in one place.

`plan` is a function of what is found on the machine and in the repository and of the answers kept:
`aleph setup` asks about the list, `aleph doctor` and the agent's status tool show it. Nothing here does
anything; the commands that do are named in each step.
"""
from __future__ import annotations

import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import dense_policy, gitio, settings, sources

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
    count = lambda number: f"{number:,} chunk" + ("" if number == 1 else "s")  # noqa: E731
    steps.append(Step(
        "vectors", "repository", "Search by meaning in this repository", vectors,
        ("needs the embedding worker, which was declined; " if no_worker else "")
        + f"{found.verdict_why}; {found.with_vector:,} of {count(found.chunks)} with a vector"
        + ("" if asked_for is None else f"; this repository is set to {'use' if asked_for else 'not use'} vectors"),
        "" if made else f"about {minutes} min of this machine's GPU for {count(missing)}; it can be interrupted and continued",
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


# ---- what is found, and the menu a person answers ------------------------------------------------


def on_terminal() -> bool:
    """A person can be asked: both ends are a terminal. An agent or a script gets the list and the flags."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _plugin_dir() -> str | None:
    """The plugin of the checkout this engine runs from; None for an installed package, which has none."""
    found = Path(__file__).resolve().parents[2] / "plugin"
    return str(found) if (found / ".claude-plugin" / "plugin.json").exists() else None


def look(status: dict) -> Found:
    """What is found, from the engine's status of the worktree (after it was indexed) and the machine."""
    from .embed import setup as embed_setup

    verdict, why = dense_policy.decide(None, status.get("main_language"))
    vectors = status.get("vectors") or {}
    return Found(claude=shutil.which("claude") is not None, plugin_dir=_plugin_dir(), worker=embed_setup.installed(),
                 language=status.get("main_language"), verdict=verdict, verdict_why=why,
                 chunks=int(vectors.get("chunks") or status.get("chunks_in_view") or 0), with_vector=int(vectors.get("with_vector") or 0),
                 sources=len(status.get("sources") or ()), untracked_cache=bool(status.get("untracked_cache")),
                 repository=bool(status.get("repository", True)))


def of(status: dict) -> list[Step]:
    """The checklist for the worktree a status was taken of. Reads the kept answers; changes nothing."""
    kept = settings.read(Path(status["index"]).parent / "settings.json")
    return plan(look(status), kept, settings.read(settings.machine_file()))


def summary(status: dict) -> dict:
    """What the agent's status tool says of it: information for a person, nothing an agent should act on."""
    return {"open": open_steps(of(status)),
            "how": "a person answers these in a terminal with `aleph setup`; search needs none of them"}


HINT = "aleph: `aleph setup` shows what else can be set up here (search by meaning, documentation from another repository). This line is shown once."


def hint_wanted() -> bool:
    """Standard error is a terminal: a person reads it. Not an agent's pipe, and never the server's log."""
    return sys.stderr.isatty()


def hint(cwd: Path) -> str | None:
    """The one line that names `aleph setup`, the first time Aleph is used in a repository on a terminal; None
    ever after, and where setup was answered. Never raises: a hint is not worth an error."""
    try:
        kept_in = settings.repo_file(gitio.common_dir(cwd))
        kept = settings.read(kept_in)
        if kept.get("hint_shown") or kept.get("setup") or "dense" in kept:
            return None
        settings.change(kept_in, hint_shown=True)
        return HINT
    except (gitio.GitError, OSError):
        return None


def answers_file(status: dict, step: Step) -> Path:
    """Where the answer to a step is kept: for the machine, or beside this repository's index."""
    return settings.machine_file() if step.scope == "machine" else Path(status["index"]).parent / "settings.json"


def text(steps: list[Step]) -> str:
    """The checklist as a person reads it."""
    lines = []
    for step in steps:
        lines.append(f"  {step.state:9} {step.title}" + ("  (this machine)" if step.scope == "machine" else ""))
        lines.append(f"            {step.detail}")
        if step.state != "done":
            if step.cost:
                lines.append(f"            costs: {step.cost}")
            lines.append(f"            by hand: {step.command}")
    return "\n".join(lines)


def as_json(steps: list[Step]) -> dict:
    return {"steps": [asdict(step) for step in steps], "open": open_steps(steps)}


class Menu:
    """The questions of `aleph setup`, one step at a time. What a yes does is given from outside (`act`), so that
    this asks and keeps answers and nothing else."""

    def __init__(self, status: dict, say, ask, act) -> None:
        self.status, self.say, self.ask, self.act = status, say, ask, act
        self.repo_file = Path(status["index"]).parent / "settings.json"

    def _yes(self, question: str, suggested: bool) -> bool:
        reply = self.ask(f"{question} {'[Y/n]' if suggested else '[y/N]'} ").strip().lower()
        return suggested if not reply else reply.startswith("y")

    def step(self, step: Step, found: Found) -> None:
        kept = answers_file(self.status, step)
        self.say(f"\n{step.title}\n  {step.detail}" + (f"\n  costs: {step.cost}" if step.cost else ""))
        if step.key == "claude":
            self.say(f"  run {step.command}\n  (Aleph does not change Claude Code's settings itself)")
            settings.answer(kept, "claude", "done" if self._yes("Is Claude Code connected, or will you run that now?", step.suggested) else "declined")
        elif step.key == "worker":
            if self._yes("Install the embedding worker and its model?", step.suggested):
                self.act("worker")                     # what was declined before is installed now: the answer is what is found
                settings.answer(kept, "worker", None)
            else:
                settings.answer(kept, "worker", "declined")
        elif step.key == "sources":
            added = 0
            while True:
                location = self.ask("A path or a URL to link (an empty line for none, or no more): ").strip()
                if not location:
                    break
                try:
                    self.say(f"  {self.act('source', location)}")
                    added += 1
                except sources.SourceError as problem:
                    self.say(f"  not linked: {problem}")
            settings.answer(kept, "sources", None if added else "declined")
        elif step.key == "vectors":
            if self._yes("Search by meaning in this repository (makes its vectors now)?", step.suggested):
                settings.change(kept, dense=True)      # kept for the repository: the vectors made are then used
                settings.answer(kept, "vectors", None)
                self.say(f"  {self.act('embed')}")
            else:
                settings.change(kept, dense=False)
        elif step.key == "untracked_cache":
            if self._yes("Switch git's untracked cache on for this repository (in its index; no configuration is changed)?", step.suggested):
                self.act("untracked_cache")
                settings.answer(kept, "untracked_cache", None)
            else:
                self.say("  by hand, for every repository of yours: git config --global core.untrackedCache true")
                settings.answer(kept, "untracked_cache", "declined")
