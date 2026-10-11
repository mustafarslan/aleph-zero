"""Linked sources: documentation (or code) that lives in another repository.

A source appears in the view as a second set of paths, `name:path`. One added by local
path is read in place and follows that clone's working tree. One added by URL is an
explicit choice to use the network: it is cloned bare into the user's cache directory
and is as fresh as the last `aleph source refresh`. A query never touches the network.

The list is kept beside the index, in `<git-common-dir>/aleph-zero/sources.json`, so
every worktree of a repository sees the same sources.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import gitio
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
    return common_dir / "aleph-zero" / "sources.json"


def _load(common_dir: Path) -> list[Source]:
    try:
        return [Source(**entry) for entry in json.loads(_file(common_dir).read_text())]
    except FileNotFoundError:
        return []
    except (ValueError, TypeError) as exc:
        raise SourceError(f"{_file(common_dir)} is not readable: {exc}") from exc


def _save(common_dir: Path, found: list[Source]) -> None:
    target = _file(common_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".json.tmp")
    scratch.write_text(json.dumps([asdict(source) for source in found], indent=2) + "\n")
    os.replace(scratch, target)  # other sessions read either the old list or the new one


def load(cwd: Path) -> list[Source]:
    return _load(gitio.common_dir(cwd))


def clone_dir(location: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "aleph-zero" / "sources" / hashlib.sha256(location.encode()).hexdigest()[:16]


def add(cwd: Path, location: str, name: str | None = None, ref: str | None = None) -> Source:
    common_dir = gitio.common_dir(cwd)
    is_url = "://" in location or location.startswith("git@")
    if name is None:
        name = Path(location.rstrip("/")).name.removesuffix(".git")
    if not _NAME.match(name):
        raise SourceError(f"a source name is letters, digits, dot, dash and underscore, not {name!r}")
    found = _load(common_dir)
    if any(source.name == name for source in found):
        raise SourceError(f"there is already a source named {name}")
    if is_url:
        source = Source(name, "url", location, ref)
        _clone(source)
    else:
        resolved = Path(location).expanduser().resolve()
        try:
            gitio.repo_info(resolved)
        except gitio.GitError as exc:
            raise SourceError(f"{location} is not a git repository") from exc
        source = Source(name, "path", str(resolved))
    _save(common_dir, [*found, source])
    return source


def remove(cwd: Path, name: str) -> None:
    common_dir = gitio.common_dir(cwd)
    found = _load(common_dir)
    kept = [source for source in found if source.name != name]
    if len(kept) == len(found):
        raise SourceError(f"no source named {name}")
    _save(common_dir, kept)
    gone = next(source for source in found if source.name == name)
    if gone.kind == "url":
        shutil.rmtree(clone_dir(gone.location), ignore_errors=True)


def refresh(cwd: Path, name: str | None = None) -> dict[str, str]:
    """Fetch the URL sources (all, or the one named). The only place the network is used
    after a source was added. Returns what happened to each."""
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
            with gitio.until(time.monotonic() + FETCH_SECONDS):
                gitio.git(clone_dir(source.location), "fetch", "--quiet", "--prune", "origin",
                          "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
            _resolve(source)
            source.last_error = ""
            report[source.name] = "unchanged" if source.tree == before else "updated"
        except gitio.GitError as exc:
            source.last_error = f"fetch failed: {str(exc)[:200]}"
            report[source.name] = f"failed: {str(exc)[:200]}"
    _save(common_dir, found)
    return report


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
            out.append(_mount(source))
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
            row["files"] = len(_mount(source).files)
        except (gitio.GitError, OSError) as exc:
            row["last_error"] = str(exc)[:200]
        if source.kind == "url" and source.fetched_at:
            row["age_seconds"] = int(time.time() - source.fetched_at)
        rows.append(row)
    return rows


def _mount(source: Source) -> Mount:
    if source.kind == "path":
        info, files, dirty = worktree_files(Path(source.location))
        return Mount(source.name, info.root, info.object_format, files, dirty)
    directory = clone_dir(source.location)
    if source.tree is None or not directory.is_dir():
        raise gitio.GitError(f"the clone of {source.location} is missing; add the source again")
    return Mount(source.name, directory, source.object_format, tree_files(directory, source.tree), frozenset())


def _clone(source: Source) -> None:
    directory = clone_dir(source.location)
    shutil.rmtree(directory, ignore_errors=True)
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gitio.until(time.monotonic() + CLONE_SECONDS):
            gitio.git(directory.parent, "clone", "--quiet", "--bare", source.location, str(directory))
        _resolve(source)
    except gitio.GitError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise SourceError(f"could not clone {source.location}: {exc}") from exc


def _resolve(source: Source) -> None:
    """Record the tree a URL source now points at."""
    directory = clone_dir(source.location)
    source.tree = gitio.git(directory, "rev-parse", "--verify", "--quiet",
                            f"{source.ref or 'HEAD'}^{{tree}}").decode().strip()
    source.object_format = gitio.git(directory, "rev-parse", "--show-object-format").decode().strip()
    source.fetched_at = time.time()
