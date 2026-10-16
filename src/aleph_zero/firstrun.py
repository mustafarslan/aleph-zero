"""Setting Aleph up the first time (ADR-013): what can be set up, as a list made in one place.

`plan` is a function of what is found on the machine and in the repository and of the answers kept:
`aleph setup` asks about the list, `aleph doctor` and the agent's status tool show it. Nothing here does
anything; the commands that do are named in each step.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import dense_policy, gitio, settings, sources
from .names import COMMAND

CHUNKS_A_SECOND = 100          # what the worker embeds on this kind of machine (ADR-005): for the estimate only
WORKER_COST = "310 MB download and a build of a few minutes; uses the network"
MEMORY_COST = "a build of under a minute (Xcode's command-line tools needed; cmake, ninja and SIMDe are fetched if missing); uses the network"


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
    aleph: str = "aleph"       # how this engine is started: where it is, when that is known
    files: int = 0             # files of the worktree itself, linked sources not counted
    plugin: bool = False       # Claude Code has the aleph-zero plugin installed: it starts the engine itself
    memory: bool = False       # Aeon's core is there: memories are recalled by meaning as well as by words
    worker_current: bool = True    # the worker is built from the llama.cpp this Aleph pins
    memory_current: bool = True    # Aeon's core is built from the commit this Aleph pins
    allowed: bool = False      # Claude Code's settings allow the plugin's engine tools: the plugin's look-ups can run


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


def _state(done: bool, answers: dict, key: str, wanted: bool = False) -> str:
    """A 'not-needed' is a no given where no was the suggestion (the machine's worker, asked in a repository whose verdict
    is against vectors): it holds where nothing wants the step, and the question is open again where something does."""
    kept = answers.get("setup", {}).get(key) if isinstance(answers.get("setup"), dict) else None
    if done or kept == "done":
        return "done"
    return "declined" if kept == "declined" or (kept == "not-needed" and not wanted) else "open"


def plan(found: Found, repo: dict, machine: dict) -> list[Step]:
    """The checklist, in the order it is asked. `repo` and `machine` are the kept answers (settings.read)."""
    asked_for = repo.get("dense") if isinstance(repo.get("dense"), bool) else None   # the person's own choice, if they made one
    wanted = found.verdict if asked_for is None else asked_for
    # `--scope user`: for every project, as the answer is kept for the machine. Claude Code's own default is one project.
    connect = (f"`claude plugin marketplace add mustafarslan/aleph-zero` and `claude plugin install aleph-zero@aleph-zero` "
               f"(with the steering), or `claude mcp add --scope user aleph-zero -- {found.aleph} serve` (the tools only)") + (
        f" (or, for one session: `claude --plugin-dir {found.plugin_dir}`)" if found.plugin_dir else "")
    steps = [
        Step("claude", "machine", "Connect Claude Code", _state(found.plugin, machine, "claude"),
             "the aleph-zero plugin is installed in Claude Code" if found.plugin
             else "`claude` is on this machine" if found.claude else "`claude` was not found on PATH",
             "", connect, found.claude),
        *([Step("allow", "machine", "Let the plugin's automatic look-ups use Aleph's tools",
                _state(found.allowed, machine, "allow"),
                "allowed in Claude Code's settings" if found.allowed
                else "not allowed yet: the plugin's look-ups (where a route or an environment variable you name is handled; "
                     "rungs 2 and 3) and its side chat do nothing until they are",
                "" if found.allowed else f"adds one rule, \"{TOOLS_RULE}\", to permissions.allow in {claude_settings_file()}; "
                                         "nothing else there is changed",
                f"{COMMAND} setup --allow-tools", True)] if found.plugin else []),
        Step("worker", "machine", "The embedding worker and its model (search by meaning)",
             "open" if found.worker and not found.worker_current else _state(found.worker, machine, "worker", wanted),
             f"installed, from an older llama.cpp than this Aleph pins: `{COMMAND} setup --worker` (or `{COMMAND} update`) builds it again"
             if found.worker and not found.worker_current
             else "installed" if found.worker else "not installed; search by meaning needs it" if wanted
             else "not installed; nothing needs it while search is by words and names",
             "" if found.worker else WORKER_COST, f"{COMMAND} embed setup", wanted),
        Step("memory", "machine", "Memory by meaning (Aeon), for recalling earlier answers",
             "declined" if not found.memory and not found.worker and _state(found.worker, machine, "worker", wanted) == "declined"
             else "open" if found.memory and not found.memory_current else _state(found.memory, machine, "memory"),
             f"installed, from an older Aeon than this Aleph pins (a newer one is pinned): `{COMMAND} setup --memory` (or `{COMMAND} update`) builds it again"
             if found.memory and not found.memory_current
             else "installed" if found.memory else "not installed; earlier answers and notes are recalled by words and recency"
             + ("" if found.worker else "; it needs the embedding worker"),
             "" if found.memory else MEMORY_COST, f"{COMMAND} setup --memory", found.worker),
    ]
    if found.repository:
        steps.append(Step(
            "sources", "repository", "Documentation or code in another repository, searched beside this one",
            _state(found.sources > 0, repo, "sources"),
            f"{found.sources} linked" if found.sources else "none linked",
            "a path is read where it is; a URL is cloned into the cache directory (uses the network)",
            f"{COMMAND} source add <path or URL>", False))
    missing = max(found.chunks - found.with_vector, 0)
    # Chosen and begun is done: chunks written since get their vectors from a running server or the next
    # `aleph embed run`, and an edit must not make this a question again. An empty repository has nothing to make.
    begun = found.with_vector > 0 or found.chunks == 0
    minutes = max(1, round(missing / CHUNKS_A_SECOND / 60))
    worker_step = next(step for step in steps if step.key == "worker")
    no_worker = not found.worker and worker_step.state == "declined"   # nothing to make vectors with, and none wanted
    vectors = "declined" if asked_for is False or no_worker else _state(begun and wanted and found.worker, repo, "vectors")
    count = lambda number: f"{number:,} chunk" + ("" if number == 1 else "s")  # noqa: E731
    steps.append(Step(
        "vectors", "repository", "Search by meaning in this repository", vectors,
        ("needs the embedding worker, which was declined; " if no_worker else "")
        + f"{found.verdict_why}; {found.with_vector:,} of {count(found.chunks)} with a vector"
        # The default is read on an issue's first line, as nearest what an agent types. A whole question is another
        # matter, and the plugin's second rung asks with the whole prompt: said here so that a yes or a no is informed.
        + ("" if found.verdict else "; on whole questions (a pasted issue, or the plugin's second rung, which asks with the "
           "whole prompt) vectors gained in every language measured, 5 to 20 points")
        + ("" if asked_for is None else f"; this repository is set to {'use' if asked_for else 'not use'} vectors")
        + (f"; `{COMMAND} embed run` makes the rest, and so does a running server" if vectors == "done" and missing else ""),
        "" if not missing else f"about {minutes} min of this machine's GPU for {count(missing)}; it can be interrupted and continued",
        f"{COMMAND} embed run", wanted))
    if found.repository:
        steps.append(Step(
            "untracked_cache", "repository", "Git's untracked cache for this worktree (a faster look at it on every query)",
            _state(found.untracked_cache, repo, "untracked_cache"),
            "on" if found.untracked_cache else "off",
            "", "git update-index --untracked-cache",
            not found.untracked_cache and found.files >= UNTRACKED_CACHE_FROM))
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


class SettingsError(OSError):
    """Claude Code's settings file could not be read as settings: it is left as it is."""


TOOLS_RULE = "mcp__plugin_aleph-zero_aleph__*"     # the plugin's engine tools, as Claude Code's permissions name them


def claude_settings_file() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def tools_allowed() -> bool:
    """Claude Code's user settings allow the plugin's engine tools (the rule, or the server's tools named one by one)."""
    try:
        allowed = (json.loads(claude_settings_file().read_text()).get("permissions") or {}).get("allow") or []
    except (OSError, ValueError, AttributeError):
        return False
    return TOOLS_RULE in allowed or "mcp__plugin_aleph-zero_aleph" in allowed


def allow_tools() -> str:
    """Add the one rule to Claude Code's user settings, on the person's yes; nothing else in the file is changed. A file
    that is not JSON is left as it is, and how to add the rule by hand is said."""
    path = claude_settings_file()
    try:
        current = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as problem:
        raise SettingsError(f"{path} is not JSON ({problem}); add \"{TOOLS_RULE}\" to permissions.allow there by hand") from None
    if not isinstance(current, dict):
        raise SettingsError(f"{path} does not hold an object; add \"{TOOLS_RULE}\" to permissions.allow there by hand")
    permissions = current.setdefault("permissions", {})
    if not isinstance(permissions, dict) or not isinstance(permissions.setdefault("allow", []), list):
        raise SettingsError(f"permissions.allow in {path} is not a list; add \"{TOOLS_RULE}\" there by hand")
    if TOOLS_RULE in permissions["allow"]:
        return f"already allowed in {path}"
    permissions["allow"].append(TOOLS_RULE)
    path.parent.mkdir(parents=True, exist_ok=True)
    written = path.with_name(path.name + ".aleph-zero.tmp")
    written.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    os.replace(written, path)
    return f"added \"{TOOLS_RULE}\" to permissions.allow in {path}; the next Claude Code session uses it"


def plugin_installed() -> bool:
    """Claude Code lists the aleph-zero plugin among its installed ones (in any scope). Read from its own file, not
    by running `claude`: this is asked on every setup list and every agent's status."""
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        listed = json.loads((root / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return False
    plugins = listed.get("plugins") if isinstance(listed, dict) else None
    return isinstance(plugins, dict) and any(name.split("@")[0] == "aleph-zero" for name in plugins)


def engine_path() -> str:
    """How this engine is started from somewhere that does not have this shell's PATH (Claude Code, a git hook):
    the `aleph` beside the Python it runs under. Under `uv run` the name alone is on PATH and elsewhere it is
    not. A Python that is an application bundle has nothing beside it: then what the shell finds, and failing
    that the name, which at least says what is missing."""
    beside = Path(sys.executable).with_name("aleph-zero")     # never `aleph`: TeX Live installs a program of that name
    if beside.exists():
        return str(beside)
    return shutil.which("aleph-zero") or "aleph-zero"


# From how many files git's untracked cache is suggested (the owner's yes, 2026-10-14): it took a question's look at
# the worktree from 36 to 13 ms on vscode (5,000 files) and from 288 to 60 on babel (27,000); on this repository (235)
# it changed nothing.
UNTRACKED_CACHE_FROM = 3000


def look(status: dict) -> Found:
    """What is found, from the engine's status of the worktree (after it was indexed) and the machine."""
    from .embed import setup as embed_setup

    verdict, why = dense_policy.decide(None, status.get("main_language"))
    vectors = status.get("vectors") or {}
    from . import memory as memories

    from . import aeon_setup

    return Found(claude=shutil.which("claude") is not None, plugin_dir=_plugin_dir(), worker=embed_setup.installed(),
                 plugin=plugin_installed(), memory=memories.aeon_available(), allowed=tools_allowed(),
                 worker_current=embed_setup.current() if embed_setup.installed() else True,
                 memory_current=aeon_setup.current() if aeon_setup.installed() else True,
                 language=status.get("main_language"), verdict=verdict, verdict_why=why,
                 chunks=int(vectors.get("chunks") or status.get("chunks_in_view") or 0), with_vector=int(vectors.get("with_vector") or 0),
                 sources=len(status.get("sources") or ()), untracked_cache=bool(status.get("untracked_cache")),
                 repository=bool(status.get("repository", True)),
                 aleph=engine_path(),
                 # the worktree's own files: a linked source's are another repository's, which git's cache here does not see
                 files=int(status.get("files_in_view") or 0) - sum(int(row.get("files") or 0) for row in status.get("sources") or ()))


def of(status: dict) -> list[Step]:
    """The checklist for the worktree a status was taken of. Reads the kept answers; changes nothing."""
    kept = settings.read(Path(status["index"]).parent / "settings.json")
    return plan(look(status), kept, settings.read(settings.machine_file()))


def summary(status: dict) -> dict:
    """What the agent's status tool says of it: information for a person, nothing an agent should act on.
    Never raises: a status that a search could answer is not lost to a list that could not be made."""
    how = f"a person answers these in a terminal with `{COMMAND} setup`; search needs none of them"
    try:
        return {"open": open_steps(of(status)), "how": how}
    except Exception as exc:  # noqa: BLE001 - an unreadable cache directory, a file of the wrong shape: whatever it is
        return {"open": [], "how": how, "unknown": f"the setup list could not be made: {exc}"}


HINT = f"aleph: `{COMMAND} setup` shows what else can be set up here (search by meaning, documentation from another repository). This line is shown once."


def hint_wanted() -> bool:
    """Standard error is a terminal: a person reads it. Not an agent's pipe, and never the server's log."""
    return sys.stderr.isatty()


def hint(cwd: Path) -> str | None:
    """The one line that names `aleph setup`, the first time Aleph is used in a repository on a terminal; None
    ever after, and where setup was answered. Never raises: a hint is not worth an error."""
    try:
        kept_in = settings.repo_file(gitio.common_dir(cwd))
        kept = settings.read(kept_in)
        if kept.get("hint_shown") or "setup" in kept or "dense" in kept:     # shown, or setup was answered in any way
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


def _failures() -> tuple:
    from .embed.setup import SetupError

    from .aeon_setup import SetupError as AeonSetupError
    from .buildtools import ToolError

    return (SetupError, AeonSetupError, ToolError, sources.SourceError, gitio.GitError, OSError)


FAILURES = _failures()     # what a step may run into without the rest of the setup being lost to it


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
        """Ask about one step and keep the answer. A yes that fails is said and left open: the questions after it
        are still asked, and this one again next time."""
        kept = answers_file(self.status, step)
        self.say(f"\n{step.title}\n  {step.detail}" + (f"\n  costs: {step.cost}" if step.cost else ""))
        try:
            self._ask(step, found, kept)
        except FAILURES as problem:
            self.say(f"  not done: {problem}\n  left open; by hand: {step.command}")

    def _ask(self, step: Step, found: Found, kept: Path) -> None:
        if step.key == "claude":
            self.say(f"  run {step.command}\n  (Aleph does not change Claude Code's settings itself)")
            settings.answer(kept, "claude", "done" if self._yes("Is Claude Code connected, or will you run that now?", step.suggested) else "declined")
        elif step.key == "allow":
            if self._yes("Allow Claude Code to use Aleph's tools without asking?", step.suggested):
                settings.answer(kept, "allow", None)
                self.say(f"  {self.act('allow')}")
            else:
                settings.answer(kept, "allow", "declined")
        elif step.key == "worker":
            if self._yes("Install the embedding worker and its model?", step.suggested):
                settings.answer(kept, "worker", None)  # what was declined before is wanted now; done is what is then found
                self.say(f"  {self.act('worker')}")
            else:
                settings.answer(kept, "worker", "declined" if step.suggested else "not-needed")
        elif step.key == "memory":
            if self._yes("Build Aeon's core, for recalling memories by meaning?", step.suggested):
                settings.answer(kept, "memory", None)
                self.say(f"  {self.act('memory')}")
            else:
                settings.answer(kept, "memory", "declined" if step.suggested else "not-needed")
        elif step.key == "sources":
            added = 0
            while True:
                location = self.ask("A path or a URL to link (an empty line for none, or no more): ").strip()
                if not location:
                    break
                try:
                    self.say(f"  {self.act('source', location)}")
                    added += 1
                except FAILURES as problem:
                    self.say(f"  not linked: {problem}")
            settings.answer(kept, "sources", None if added else "declined")
        elif step.key == "vectors":
            if self._yes("Search by meaning in this repository (makes its vectors now)?", step.suggested):
                settings.change(kept, dense=True)      # kept for the repository: the vectors made are then used
                settings.answer(kept, "vectors", None)
                self.say(f"  {self.act('embed')}")
            elif found.verdict:
                settings.change(kept, dense=False)     # against the verdict: the repository's own choice
            else:
                settings.answer(kept, "vectors", "declined")   # the verdict already says no: an answer, not a choice
        elif step.key == "untracked_cache":
            if self._yes("Switch git's untracked cache on for this worktree (in its index; no configuration is changed)?", step.suggested):
                settings.answer(kept, "untracked_cache", None)
                self.say(f"  {self.act('untracked_cache')}")
            else:
                settings.answer(kept, "untracked_cache", "declined")
