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

_TREE_CACHE: dict[tuple[str, str], tuple[dict[str, str], tuple[str, ...]]] = {}
_TREE_CACHE_SIZE = 8
# Per worktree with a sparse checkout: what its files were filtered from, and the result.
_SPARSE_CACHE: dict[Path, tuple[tuple, dict[str, str], int]] = {}


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
    mounted: frozenset[str] = frozenset()  # the paths of `files` that come from a linked source
    outside_checkout: int = 0              # tracked files a sparse checkout leaves out; they are not searched
    submodules: tuple[str, ...] = ()       # paths of submodules; their content is not searched

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
        if path in self.mounted:
            name, _, inside = path.partition(":")
            mount = self._mounted[name]
            return mount.root, inside, mount.object_format
        return self.root, path, self.object_format  # also a real file that is spelled `name:path`

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


def _tree(root: Path, common_dir: Path, tree: str) -> tuple[dict[str, str], tuple[str, ...]]:
    key = (str(common_dir), tree)
    if key not in _TREE_CACHE:
        if len(_TREE_CACHE) >= _TREE_CACHE_SIZE:
            _TREE_CACHE.pop(next(iter(_TREE_CACHE)))
        _TREE_CACHE[key] = gitio.read_tree(root, tree)
    return _TREE_CACHE[key]


def _tree_files(root: Path, common_dir: Path, tree: str) -> dict[str, str]:
    return _tree(root, common_dir, tree)[0]


def _stamp(path: Path) -> tuple[int, int]:
    try:
        found = path.stat()
        return found.st_mtime_ns, found.st_size
    except OSError:
        return 0, 0


def _checked_out(info: gitio.RepoInfo, files: dict[str, str]) -> tuple[dict[str, str], int]:
    """`files` without what a sparse checkout leaves out, and how many that is.

    A sparse checkout is the user's statement of what they work on. What is outside it is
    not on disk, cannot be opened by the agent, and in a partial clone is not even in the
    object store. Git's index says which files those are; it is asked again only when the
    index or the sparse-checkout file has changed."""
    git_dir = info.git_dir
    sparse_file = git_dir / "info" / "sparse-checkout" if git_dir is not None else None
    if sparse_file is None or not sparse_file.exists():
        return files, 0  # the usual case costs one stat
    key = (info.tree, _stamp(git_dir / "index"), _stamp(sparse_file))
    cached = _SPARSE_CACHE.get(info.root)
    if cached is not None and cached[0] == key:
        return dict(cached[1]), cached[2]
    skipped, directories = gitio.skip_worktree(info.root)
    kept = files
    if skipped or directories:
        prefixes = frozenset(directories)

        def outside(path: str) -> bool:
            if path in skipped:
                return True
            end = path.find("/")
            while prefixes and end != -1:
                if path[:end + 1] in prefixes:
                    return True
                end = path.find("/", end + 1)
            return False

        kept = {path: oid for path, oid in files.items() if not outside(path)}
    _SPARSE_CACHE[info.root] = (key, kept, len(files) - len(kept))
    return dict(kept), len(files) - len(kept)


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


@dataclass(frozen=True)
class Worktree:
    """One worktree as it is now."""
    info: gitio.RepoInfo
    files: dict[str, str]          # path -> blob id
    dirty: frozenset[str]          # the paths whose content was taken from the working copy
    outside_checkout: int = 0
    submodules: tuple[str, ...] = ()


def read_worktree(cwd: Path) -> Worktree:
    info = gitio.repo_info(cwd)
    root, fmt, tree = info.root, info.object_format, info.tree
    submodules: tuple[str, ...] = ()
    files: dict[str, str] = {}
    outside = 0
    if tree:
        tracked, submodules = _tree(root, info.common_dir, tree)
        files, outside = _checked_out(info, tracked)
        if files is tracked:
            files = dict(tracked)
    dirty: set[str] = set()
    listings: dict[Path, frozenset[str]] = {}
    for number, path in enumerate(gitio.status(root)):
        if number % 64 == 0:
            gitio.check_deadline()  # thousands of changed files are hashed here, outside any git call
        oid = _dirty_blob(root, path, fmt, listings)
        if oid is None:
            files.pop(path, None)  # deleted, renamed away, unreadable, or too large to index
        else:
            files[path] = oid
            dirty.add(path)
    return Worktree(info, files, frozenset(dirty), outside, submodules)


def worktree_files(cwd: Path) -> tuple[gitio.RepoInfo, dict[str, str], frozenset[str]]:
    """One worktree as it is now: its repository facts, path -> blob id, and the paths
    whose content was taken from the working copy."""
    found = read_worktree(cwd)
    return found.info, found.files, found.dirty


def tree_files(root: Path, tree: str) -> dict[str, str]:
    """The files of a tree in a repository, remembered per tree."""
    return _tree_files(root, root, tree)


def build_view(cwd: Path) -> View:
    from . import sources  # sources builds its mounts with the helpers above

    worktree = read_worktree(cwd)
    info, files, dirty = worktree.info, worktree.files, worktree.dirty
    mounts = tuple(sources.mounts(info.common_dir))
    mounted: set[str] = set()
    if mounts:
        dirty = set(dirty)
        for mount in mounts:
            for inside, oid in mount.files.items():
                path = f"{mount.name}:{inside}"
                if path in files:
                    continue  # a real file of that spelling wins; the source's file is hidden
                files[path] = oid
                mounted.add(path)
                if inside in mount.dirty:
                    dirty.add(path)
        dirty = frozenset(dirty)
    return View(info.root, info.common_dir, info.object_format, info.tree, files, dirty, mounts, frozenset(mounted),
                worktree.outside_checkout, worktree.submodules)
