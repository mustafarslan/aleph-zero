"""Is everything Aleph needs in place? `aleph doctor` prints the answer."""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import apsw

from . import firstrun, gitio, indexer, indexers, lang, plain
from .chunking import CHUNKER_VERSION
from .lang import parse
from .store import SCHEMA_VERSION, index_file, state_reason
from .store import db as store_db
from .store.db import MIN_SQLITE
from .view import build_view


@dataclass(frozen=True)
class Row:
    level: str   # 'ok', 'note' (worth knowing, nothing is broken) or 'FAIL'
    name: str
    detail: str


def check(cwd: Path) -> list[Row]:
    rows = [_git(), _sqlite(), _tree_sitter(), _grammars(), _worker(), _compiler_indexes()]
    try:
        info = gitio.repo_info(cwd)
    except gitio.NoRepository:
        return rows + _plain_directory(cwd)
    except gitio.GitError:
        rows.append(Row("note", "repository", "not inside a git repository: nothing to check for this directory"))
        return rows
    rows.append(_index(index_file(info.common_dir)))
    if state_reason(info.common_dir):
        rows.append(Row("note", "index location", f"the index is kept in the cache directory: {state_reason(info.common_dir)}"))
    rows.append(_untracked_cache(info.root))
    rows.append(_setup(info.root))
    rows.extend(_checkout(cwd))
    return rows


def _checkout(cwd: Path) -> list[Row]:
    """What of the repository is not searched, and why."""
    from .view import read_worktree

    try:
        worktree = read_worktree(cwd)
    except gitio.GitError as exc:
        return [Row("FAIL", "worktree", f"the worktree cannot be read: {exc}")]
    rows = []
    if worktree.outside_checkout:
        rows.append(Row("note", "sparse checkout", f"sparse checkout: {worktree.outside_checkout} tracked files are "
                        "outside it and are not searched"))
    try:
        promisor = gitio.git(worktree.info.root, "config", "--get-regexp", r"^(extensions\.partialclone|remote\..*\.promisor)$")
    except gitio.GitError:
        promisor = b""  # git exits 1 when no such setting exists: a full clone
    if promisor.strip():
        rows.append(Row("note", "partial clone", "partial clone: a file whose content git has not fetched is not searched, "
                        "and nothing is fetched to answer a query; `aleph status` counts such files (unreadable)"))
    if worktree.submodules:
        listed = ", ".join(worktree.submodules[:5]) + (" ..." if len(worktree.submodules) > 5 else "")
        rows.append(Row("note", "submodules", f"submodules are not searched ({listed}); link one as a source "
                        f"with `aleph source add {worktree.submodules[0]}`"))
    return rows


def _compiler_indexes() -> Row:
    """Which SCIP indexers are installed, and which languages `aleph scip run` may run one for."""
    parts = []
    for indexer, found in indexers.present():
        how = ("on request" + (f" ({indexer.note})" if indexer.note else "")) if indexer.on_demand else f"import-only unless ALEPH_SCIP_MEMORY_MB names a limit ({indexer.why_not})"
        parts.append(f"{indexer.language}: {indexer.program} {'found' if found else 'not installed'}, {how}")
    try:
        allowed = f"A run may use {indexers.limit_mb():.0f} MB (ALEPH_SCIP_MEMORY_MB)"
    except indexers.CannotRun as exc:
        allowed = f"No run is possible: {exc}"
    return Row("note", "compiler indexes", "SCIP indexers for `aleph scip run`: " + "; ".join(parts) + ". " + allowed)


def _git() -> Row:
    try:
        version = subprocess.run(["git", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        return Row("FAIL", "git", f"git cannot be run: {exc}")
    return Row("ok", "git", version.replace("git version", "git"))


def _sqlite() -> Row:
    version = apsw.sqlite_lib_version()
    if tuple(int(part) for part in version.split(".")) < MIN_SQLITE:
        return Row("FAIL", "SQLite", f"SQLite {version} is older than {'.'.join(map(str, MIN_SQLITE))}; reinstall apsw")
    return Row("ok", "SQLite", f"SQLite {version} (through apsw)")


def _tree_sitter() -> Row:
    version = metadata.version("tree-sitter")
    if version.startswith("0.26."):
        return Row("FAIL", "tree-sitter", f"tree-sitter {version} crashes on large files; this project needs 0.25.x (run `uv sync`)")
    return Row("ok", "tree-sitter", f"tree-sitter {version}")


def _plain_directory(cwd: Path) -> list[Row]:
    """A directory that is not a repository: whether it is searched, what of it is not, and where its index is."""
    try:
        view = build_view(cwd)
    except gitio.GitError as exc:       # refused by the rule, or a repository git cannot read
        return [Row("note", "repository", f"{exc}")]
    inside = f"; {view.inner_repositories} repositories inside it are not searched from here" if view.inner_repositories else ""
    return [Row("note", "repository", f"{view.root} is not a git repository: its {len(view.files)} files are searched as they are on "
                                      f"disk, this directory and below only. Not searched: {plain.WHAT_IS_LEFT_OUT}{inside}. "
                                      f"`git init` makes it a repository, where a `.gitignore` decides"),
            _index(index_file(view.common_dir)),
            Row("note", "index location", "the index is kept in the cache directory: nothing is written into a directory that is not a repository")]


def _grammars() -> Row:
    wanted = sorted({profile.grammar: profile for profile in lang.PROFILES.values()}.items())
    missing = []
    for grammar, profile in wanted:
        try:
            parse.language(profile)
        except Exception:  # noqa: BLE001 - whatever the reason, the grammar is not usable
            missing.append(grammar)
    have = len(wanted) - len(missing)
    if missing:
        return Row("FAIL", "grammars", f"grammars: {have} of {len(wanted)} load; missing: {', '.join(missing)}. "
                                       "Run `aleph doctor --fetch-grammars` (uses the network)")
    return Row("ok", "grammars", f"grammars: {have} of {len(wanted)} load")


def _worker() -> Row:
    from .embed import setup, worker  # only doctor and the embed commands look at these

    if not setup.installed():
        return Row("note", "embedding worker", "embedding worker: not installed, so search is by words and names only. "
                                               "`aleph embed setup` builds it and downloads the model (310 MB, uses the network)")
    running = "running" if worker.available() else "not running (it starts when vectors are needed)"
    return Row("ok", "embedding worker", f"embedding worker: installed in {worker.home()}, {running}")


def _index(path: Path) -> Row:
    if not path.exists():
        return Row("note", "index", f"index: not built yet; the first search or `aleph index` creates {path}")
    try:
        conn = apsw.Connection(str(path), flags=apsw.SQLITE_OPEN_READONLY)
        try:
            verdict = conn.execute("PRAGMA quick_check").fetchall()[0][0]
            version = conn.pragma("user_version")
            chunker = conn.execute("SELECT max(chunker_version) FROM blobs").fetchall()[0][0] if version == SCHEMA_VERSION else None
        finally:
            conn.close()
    except apsw.Error as exc:
        return Row("FAIL", "index", f"index: {path} cannot be read ({type(exc).__name__}); it is a cache: the next search sets it aside and builds a new one")
    if verdict != "ok":
        return Row("FAIL", "index", f"index: {path} is damaged ({verdict}); it is a cache: the next search sets it aside and builds a new one")
    if chunker is not None and chunker > CHUNKER_VERSION:      # searches are refused: say so where one looks for why
        return Row("FAIL", "index", f"index: {indexer.newer_build(str(path), chunker)}")
    if version == SCHEMA_VERSION:
        age = ""
    elif version in store_db.upgradable():
        age = f", schema {version} (this build writes {SCHEMA_VERSION} and brings it up to date on first use; nothing is lost)"
    else:
        age = f", schema {version} (this build writes {SCHEMA_VERSION} and will rebuild it)"
    return Row("ok", "index", f"index: {path} ({path.stat().st_size / 1e6:.1f} MB{age}{_vector_file(path)})")


def _vector_file(index: Path) -> str:
    """The vector files beside the index, in a few words."""
    found = sorted(index.parent.glob("vectors.*.f16"))
    if not found:
        return ""
    size = sum(path.stat().st_size for path in found)
    held = sum(path.stat().st_blocks * 512 for path in found)
    if held < size * 0.9:       # blocks of free rows were given back: as long as before, smaller on disk
        return _vector_rows(index, f"; vectors {size / 1e6:.1f} MB long, {held / 1e6:.1f} MB on disk")
    return _vector_rows(index, f"; vectors {size / 1e6:.1f} MB")


def _vector_rows(index: Path, said: str) -> str:
    try:
        conn = apsw.Connection(str(index), flags=apsw.SQLITE_OPEN_READONLY)
        try:
            free = conn.execute("SELECT count(*) FROM vector_free").fetchall()[0][0]
            used = conn.execute("SELECT count(*) FROM chunk_vectors").fetchall()[0][0]
        finally:
            conn.close()
    except apsw.Error:
        return said
    return f"{said}, {used} rows in use, {free} free to be used again"


def _setup(root: Path) -> Row:
    """What `aleph setup` would still ask (ADR-013). Shown here; only that command asks."""
    from .engine import Engine

    try:
        steps = firstrun.of(Engine(root).status())
    except Exception as exc:  # noqa: BLE001 - the doctor reports, it does not fall over what it looks at
        return Row("note", "setup", f"the setup list could not be made: {exc}")
    waiting = [step.title for step in steps if step.state == "open"]
    if not waiting:
        return Row("ok", "setup", "nothing is open (`aleph setup --again` asks about what was declined)")
    return Row("note", "setup", f"{len(waiting)} of {len(steps)} things are open: {'; '.join(waiting)}. "
                                "`aleph setup` asks about them once; search needs none of them")


def _untracked_cache(root: Path) -> Row:
    started = time.perf_counter()
    build_view(root)
    took = (time.perf_counter() - started) * 1000
    if gitio.untracked_cache_enabled(root):
        return Row("ok", "untracked cache", f"git's untracked cache is on; looking at the worktree took {took:.0f} ms")
    return Row("note", "untracked cache",
               f"git's untracked cache is off; looking at the worktree took {took:.0f} ms, on every query. "
               "It is usually two to three times faster with `git config core.untrackedCache true` "
               "(your choice: it changes this repository's git configuration), or with "
               "`git update-index --untracked-cache`, which keeps the cache in this repository's index and "
               "changes no configuration")


def text(rows: list[Row]) -> str:
    return "\n".join(f"{row.level:<5} {row.detail}" for row in rows)
