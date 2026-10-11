"""A view is what one worktree looks like right now: path -> blob id.

Clean files come from HEAD's tree. Files that differ on disk (modified, staged,
untracked) are hashed from the working copy; deleted files leave the map. A search
hit is valid only if its blob is in the view, so results cannot be stale.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from . import filters, gitio

_TREE_CACHE: dict[tuple[str, str], dict[str, str]] = {}
_TREE_CACHE_SIZE = 8


@dataclass(frozen=True)
class View:
    root: Path
    common_dir: Path
    object_format: str
    tree: str | None
    files: dict[str, str]
    dirty: frozenset[str]

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

    def paths_for(self, oid: str) -> list[str]:
        return self._paths_by_blob.get(oid, [])

    def read(self, path: str, reader: gitio.BlobReader) -> bytes | None:
        """Content of a file in the view: from disk if it differs from HEAD, else from git."""
        if path in self.dirty:
            try:
                return (self.root / path).read_bytes()
            except OSError:
                return None
        return reader.read(self.files[path])


def _tree_files(root: Path, common_dir: Path, tree: str) -> dict[str, str]:
    key = (str(common_dir), tree)
    if key not in _TREE_CACHE:
        if len(_TREE_CACHE) >= _TREE_CACHE_SIZE:
            _TREE_CACHE.pop(next(iter(_TREE_CACHE)))
        _TREE_CACHE[key] = gitio.ls_tree(root, tree)
    return _TREE_CACHE[key]


def build_view(cwd: Path) -> View:
    info = gitio.repo_info(cwd)
    root, fmt, tree = info.root, info.object_format, info.tree
    files = dict(_tree_files(root, info.common_dir, tree)) if tree else {}
    dirty: set[str] = set()
    for path in gitio.status(root):
        full = root / path
        if full.is_file() and not full.is_symlink() and full.stat().st_size <= filters.MAX_BYTES:
            files[path] = gitio.hash_blob(full.read_bytes(), fmt)
            dirty.add(path)
        else:
            files.pop(path, None)  # deleted, replaced by a directory, or too large to index
    return View(root, info.common_dir, fmt, tree, files, frozenset(dirty))
