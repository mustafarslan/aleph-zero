"""Linked sources: documentation (or code) that lives in another repository.

A source appears in the view as a second set of paths, `name:path`. One added by local
path is read in place and follows that clone's working tree. One added by URL is an
explicit choice to use the network: it is cloned bare into the user's cache directory
and is as fresh as the last `aleph source refresh`. A query never touches the network.

The list is kept beside the index (`store.state_dir`), so
every worktree of a repository sees the same sources.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path

from . import gitio
from .store import state_dir
from .view import Mount, tree_files, worktree_files

CLONE_SECONDS = 300
FETCH_SECONDS = 60
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class SourceError(ValueError):
    """A source cannot be added, removed or read as asked."""


@dataclass
class Source:
    name: str
    kind: str                  # 'path' or 'url'
    location: str              # a directory, or a URL
    ref: str | None = None     # url only: the branch or tag to read; None for the remote's default
    tree: str | None = None    # url only: the tree last fetched
    object_format: str = "sha1"
    fetched_at: float = 0.0    # url only: when the last successful fetch finished
    last_error: str = ""


def _file(common_dir: Path) -> Path:
    return state_dir(common_dir) / "sources.json"


def _load(common_dir: Path) -> list[Source]:
    try:
        entries = json.loads(_file(common_dir).read_text())
        known = Source.__dataclass_fields__
        return [Source(**{key: value for key, value in entry.items() if key in known}) for entry in entries]
    except FileNotFoundError:
        return []
    except (ValueError, TypeError, AttributeError) as exc:
        raise SourceError(f"{_file(common_dir)} is not readable: {exc}") from exc


def _save(common_dir: Path, found: list[Source]) -> None:
    target = _file(common_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A scratch file of this writer's own: two writers sharing one name replace each other's half-written file.
    scratch = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    scratch.write_text(json.dumps([asdict(source) for source in found], indent=2) + "\n")
    os.replace(scratch, target)  # other sessions read either the old list or the new one


@contextmanager
def _one_writer(common_dir: Path):
    """Read the list, change it and write it as one step. Every worktree of a repository shares the list, and a
    fetch takes up to a minute: without this, the later of two writers puts back the list as it read it and the
    other's change (a source added meanwhile, another worktree's refresh) is gone. Never held across the network."""
    directory = state_dir(common_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "sources.json.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load(cwd: Path) -> list[Source]:
    return _load(gitio.common_dir(cwd))


def clone_dir(common_dir: Path, location: str) -> Path:
    """Where a URL source of one repository is cloned. The repository is part of the name:
    two repositories linking the same URL each keep their own clone, so removing or
    re-adding a source in one cannot take it away from the other."""
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    key = hashlib.sha256(f"{common_dir}\0{location}".encode()).hexdigest()[:16]
    return base / "aleph-zero" / "sources" / key


def add(cwd: Path, location: str, name: str | None = None, ref: str | None = None) -> Source:
    common_dir = gitio.common_dir(cwd)
    is_url = "://" in location or location.startswith("git@")
    if name is None:
        name = Path(location.rstrip("/")).name.removesuffix(".git")
    if not _NAME.match(name):
        raise SourceError(f"a source name is letters, digits, dot, dash and underscore, not {name!r}")
    if any(source.name == name for source in _load(common_dir)):
        raise SourceError(f"there is already a source named {name}")
    if is_url:
        source = Source(name, "url", location, ref)
        _clone(common_dir, source)
    else:
        resolved = Path(location).expanduser().resolve()
        try:
            with gitio.elsewhere():
                linked = gitio.repo_info(resolved)
        except gitio.GitError as exc:
            raise SourceError(f"{location} is not a git repository") from exc
        if linked.common_dir == common_dir:
            raise SourceError(f"{location} is this repository; its files are searched already")
        source = Source(name, "path", str(linked.root))  # the whole repository that contains the path
    with _one_writer(common_dir):
        found = _load(common_dir)                        # as it is now: the clone above may have taken minutes
        if any(other.name == name for other in found):
            raise SourceError(f"there is already a source named {name}")
        _save(common_dir, [*found, source])
    return source


def remove(cwd: Path, name: str) -> None:
    common_dir = gitio.common_dir(cwd)
    with _one_writer(common_dir):
        found = _load(common_dir)
        kept = [source for source in found if source.name != name]
        if len(kept) == len(found):
            raise SourceError(f"no source named {name}")
        _save(common_dir, kept)
    gone = next(source for source in found if source.name == name)
    if gone.kind == "url":
        shutil.rmtree(clone_dir(common_dir, gone.location), ignore_errors=True)


def refresh(cwd: Path, name: str | None = None, unattended: bool = False) -> dict[str, str]:
    """Fetch the URL sources (all, or the one named). The only place the network is used
    after a source was added. Returns what happened to each. With `unattended` (a hook, a
    schedule) git never asks for a name or a password and a failure says how old the state is
    that goes on being searched."""
    common_dir = gitio.common_dir(cwd)
    found = _load(common_dir)
    if name is not None and not any(source.name == name for source in found):
        raise SourceError(f"no source named {name}")
    report: dict[str, str] = {}
    for source in found:
        if name is not None and source.name != name:
            continue
        if source.kind != "url":
            report[source.name] = "local: read in place, nothing to fetch"
            continue
        before = source.tree
        try:
            with gitio.until(time.monotonic() + FETCH_SECONDS), gitio.elsewhere(), (gitio.unattended() if unattended else nullcontext()):
                gitio.git(clone_dir(common_dir, source.location), "fetch", "--quiet", "--prune", "origin",
                          "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
                _resolve(common_dir, source)             # under the same time limit, and as unattended as the fetch
            source.last_error = ""
            report[source.name] = "unchanged" if source.tree == before else "updated"
        except gitio.GitError as exc:
            source.last_error = f"fetch failed: {str(exc)[:200]}"
            report[source.name] = f"failed: {str(exc)[:200]}" + (
                f"; what is searched was fetched {_ago(time.time() - source.fetched_at)} ago" if unattended and source.fetched_at else "")
        # What this fetch learned goes into the list as it is NOW, one source at a time: a source added or
        # removed during the fetch stays as it is, and a refresh that is stopped half way keeps what it did.
        with _one_writer(common_dir):
            now = _load(common_dir)
            for other in now:
                if other.name == source.name and other.location == source.location:
                    other.tree, other.object_format = source.tree, source.object_format
                    other.fetched_at, other.last_error = source.fetched_at, source.last_error
            _save(common_dir, now)
    return report


def _ago(seconds: float) -> str:
    return f"{seconds / 86400:.0f} days" if seconds >= 172800 else f"{seconds / 3600:.0f} h" if seconds >= 7200 else f"{max(seconds, 60) / 60:.0f} min"


def mounts(common_dir: Path) -> list[Mount]:
    """The sources that can be read right now, as parts of a view. One that cannot (its
    directory was moved, its clone is gone) is left out: search goes on without it, and
    `describe` says why."""
    out: list[Mount] = []
    try:
        found = _load(common_dir)
    except SourceError:
        return out
    for source in found:
        try:
            out.append(_mount(common_dir, source))
        except (gitio.GitError, OSError):
            continue
    return out


def describe(common_dir: Path) -> list[dict]:
    """Each source with what a person needs to know about it: how many files it
    contributes, how old a fetched one is, and what is wrong with it if anything."""
    rows = []
    for source in _load(common_dir):
        row = {"name": source.name, "kind": source.kind, "location": source.location, "ref": source.ref,
               "files": 0, "age_seconds": None, "last_error": source.last_error}
        try:
            row["files"] = len(_mount(common_dir, source).files)
        except (gitio.GitError, OSError) as exc:
            row["last_error"] = str(exc)[:200]
        if source.kind == "url" and source.fetched_at:
            row["age_seconds"] = int(time.time() - source.fetched_at)
        rows.append(row)
    return rows


def _mount(common_dir: Path, source: Source) -> Mount:
    # Another repository than the one Aleph was pointed at: its git is not told by the environment which one it is.
    with gitio.elsewhere():
        if source.kind == "path":
            info, files, dirty = worktree_files(Path(source.location))
            return Mount(source.name, info.root, info.object_format, files, dirty)
        directory = clone_dir(common_dir, source.location)
        if source.tree is None or not directory.is_dir():
            raise gitio.GitError(f"the clone of {source.location} is missing; add the source again")
        return Mount(source.name, directory, source.object_format, tree_files(directory, source.tree), frozenset())


def _clone(common_dir: Path, source: Source) -> None:
    directory = clone_dir(common_dir, source.location)
    shutil.rmtree(directory, ignore_errors=True)
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gitio.until(time.monotonic() + CLONE_SECONDS), gitio.elsewhere():
            gitio.git(directory.parent, "clone", "--quiet", "--bare", "--", source.location, str(directory))
        _resolve(common_dir, source)
    except gitio.GitError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise SourceError(f"could not clone {source.location}: {exc}") from exc


def _resolve(common_dir: Path, source: Source) -> None:
    """Record the tree a URL source now points at."""
    directory = clone_dir(common_dir, source.location)
    with gitio.elsewhere():
        source.tree = gitio.git(directory, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                f"{source.ref or 'HEAD'}^{{tree}}").decode().strip()
        source.object_format = gitio.git(directory, "rev-parse", "--show-object-format").decode().strip()
    source.fetched_at = time.time()
