"""A view is what one worktree looks like right now: path -> blob id.

Clean files come from HEAD's tree. Files that differ on disk (modified, staged,
untracked) are hashed from the working copy; deleted files leave the map. A search
hit is valid only if its blob is in the view, so results cannot be stale.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from . import filters, gitio

_TREE_CACHE: dict[tuple[str, str], dict[str, str]] = {}
_TREE_CACHE_SIZE = 8


@dataclass(frozen=True)
class Mount:
    """A linked source inside a view: another repository whose files appear as `name:path`."""
    name: str
    root: Path                 # a worktree, or a bare repository
    object_format: str
    files: dict[str, str]      # path inside the source -> blob id
    dirty: frozenset[str]      # paths read from the source's working copy


@dataclass(frozen=True)
class View:
    root: Path
    common_dir: Path
    object_format: str
    tree: str | None
    files: dict[str, str]      # every path of the view, linked sources included as `name:path`
    dirty: frozenset[str]
    mounts: tuple[Mount, ...] = ()

    @cached_property
    def fingerprint(self) -> str:
        digest = hashlib.blake2b(digest_size=16)
        for path in sorted(self.files):
            digest.update(path.encode("utf-8", "surrogateescape") + b"\0" + self.files[path].encode() + b"\0")
        return digest.hexdigest()

    @cached_property
    def _paths_by_blob(self) -> dict[str, list[str]]:
        by_blob: dict[str, list[str]] = {}
        for path in sorted(self.files):
            by_blob.setdefault(self.files[path], []).append(path)
        return by_blob

    @cached_property
    def _mounted(self) -> dict[str, Mount]:
        return {mount.name: mount for mount in self.mounts}

    def paths_for(self, oid: str) -> list[str]:
        return self._paths_by_blob.get(oid, [])

    def locate(self, path: str) -> tuple[Path, str, str]:
        """Where a path of the view really is: (repository, path inside it, object format)."""
        name, colon, inside = path.partition(":")
        mount = self._mounted.get(name) if colon else None
        if mount is not None and inside in mount.files:
            return mount.root, inside, mount.object_format
        return self.root, path, self.object_format

    def read(self, path: str, readers: gitio.Readers,
             max_bytes: int | None = None) -> bytes | gitio.TooLarge | None:
        """Content of a file exactly as the view saw it: from disk if it differs from HEAD,
        else from git. None if it cannot be read that way any more: gone, unreadable, or
        edited since. The caller must not treat None as a statement about the content."""
        root, inside, fmt = self.locate(path)
        if path in self.dirty:
            try:
                data = (root / inside).read_bytes()
            except OSError:
                return None
            return data if gitio.hash_blob(data, fmt) == self.files[path] else None
        return readers.reader(root).read(self.files[path], max_bytes)


def _tree_files(root: Path, common_dir: Path, tree: str) -> dict[str, str]:
    key = (str(common_dir), tree)
    if key not in _TREE_CACHE:
        if len(_TREE_CACHE) >= _TREE_CACHE_SIZE:
            _TREE_CACHE.pop(next(iter(_TREE_CACHE)))
        _TREE_CACHE[key] = gitio.ls_tree(root, tree)
    return _TREE_CACHE[key]


def _spelled_as_on_disk(root: Path, path: str, listings: dict[Path, frozenset[str]]) -> bool:
    """False if the file system holds this path under another spelling. On a
    case-insensitive volume a renamed file still answers to its old name."""
    directory = root
    for part in path.split("/"):
        if directory not in listings:
            try:
                listings[directory] = frozenset(os.listdir(directory))
            except OSError:
                listings[directory] = frozenset()
        if part not in listings[directory]:
            return False
        directory = directory / part
    return True


def _dirty_blob(root: Path, path: str, fmt: str, listings: dict[Path, frozenset[str]]) -> str | None:
    """The blob id of a working-copy file, or None if it is not an indexable file right now."""
    full = root / path
    try:
        if full.is_symlink() or not full.is_file() or full.stat().st_size > filters.MAX_BYTES:
            return None
        if not _spelled_as_on_disk(root, path, listings):
            return None
        return gitio.hash_blob(full.read_bytes(), fmt)
    except OSError:  # unreadable, or removed while we looked
        return None


def worktree_files(cwd: Path) -> tuple[gitio.RepoInfo, dict[str, str], frozenset[str]]:
    """One worktree as it is now: its repository facts, path -> blob id, and the paths
    whose content was taken from the working copy."""
    info = gitio.repo_info(cwd)
    root, fmt, tree = info.root, info.object_format, info.tree
    files = dict(_tree_files(root, info.common_dir, tree)) if tree else {}
    dirty: set[str] = set()
    listings: dict[Path, frozenset[str]] = {}
    for path in gitio.status(root):
        oid = _dirty_blob(root, path, fmt, listings)
        if oid is None:
            files.pop(path, None)  # deleted, renamed away, unreadable, or too large to index
        else:
            files[path] = oid
            dirty.add(path)
    return info, files, frozenset(dirty)


def tree_files(root: Path, tree: str) -> dict[str, str]:
    """The files of a tree in a repository, remembered per tree."""
    return _tree_files(root, root, tree)


def build_view(cwd: Path) -> View:
    from . import sources  # sources builds its mounts with the helpers above

    info, files, dirty = worktree_files(cwd)
    mounts = tuple(sources.mounts(info.common_dir))
    if mounts:
        dirty = set(dirty)
        for mount in mounts:
            for inside, oid in mount.files.items():
                files.setdefault(f"{mount.name}:{inside}", oid)  # a real path of that spelling wins
            dirty.update(f"{mount.name}:{inside}" for inside in mount.dirty)
        dirty = frozenset(dirty)
    return View(info.root, info.common_dir, info.object_format, info.tree, files, dirty, mounts)
