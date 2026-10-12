"""A directory that is not a git repository, as a view: its files as they are on disk.

A repository says what belongs to it; a directory does not. So this walks only where it is
plain that a project was meant (never a home directory, never the top of a disk), stops and
refuses when the directory is larger than a project is, and leaves out what nothing vouches
for: hidden files (without a `.gitignore`, nothing else says `.env` is not source), the
directories tools fill, and repositories inside it, which have indexes of their own.

Git remembers what it hashed; here a file beside the index does. A file is hashed again when
its size, its modification time or its change time differs from what was remembered, or when
its change time is not older than the walk that remembered it (written and hashed in one tick
of the clock, it could differ and look the same). Nothing is written into the directory.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import filters, gitio
from .store import db as store_db

FILE_LIMIT = 20_000         # files that would be searched
ENTRY_LIMIT = 200_000       # directory entries looked at to find them
LEFT_OUT = frozenset({"node_modules", "__pycache__", "venv"})    # with everything hidden, and what a CACHEDIR.TAG marks
MEMORY = "hashed.json"
WHAT_IS_LEFT_OUT = ("hidden files and directories, node_modules, __pycache__, venv, directories marked CACHEDIR.TAG, "
                    "symbolic links and repositories inside it")
_REMEMBERED: dict[Path, tuple[tuple[int, int], dict[str, list]]] = {}    # per directory: the memory file's stamp, and its content


class Refused(gitio.NotARepository):
    """A directory without git that is not walked, or was found too large to be one project."""


def refusal(root: Path) -> str | None:
    """Why a (resolved) directory is not walked at all; None when it may be."""
    home = Path.home().resolve()
    if root == home or root in home.parents:
        return (f"{root} is not inside a git repository, and it is your home directory or holds it: Aleph does not walk "
                f"that. Start it in a project's directory, or run `git init` in one")
    if len(root.parts) <= 2 or root == Path(tempfile.gettempdir()).resolve():
        return (f"{root} is not inside a git repository, and it is the top of a disk or a system directory: Aleph does "
                f"not walk that. Start it in a project's directory")
    return None


def read(directory: Path) -> tuple[Path, Path, dict[str, str], int]:
    """(the directory, where its state is kept, path -> blob id of every file searched, repositories inside
    that were not descended into). Raises Refused where the rule says so, before or while walking."""
    root = directory.resolve()
    why = refusal(root)
    if why:
        raise Refused(why)
    kept = store_db.cache_dir_for(root)
    remembered = _remembered(root, kept / MEMORY)
    started = time.time_ns()
    files: dict[str, str] = {}
    now: dict[str, list] = {}
    inner = looked = 0
    changed = False
    pending = [root]
    while pending:
        here = pending.pop()
        try:
            with os.scandir(here) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            continue        # unreadable, or gone while we looked
        for entry in entries:
            looked += 1
            if looked % 64 == 0:
                gitio.check_deadline()
            if looked > ENTRY_LIMIT:
                raise Refused(_too_large(root, f"more than {ENTRY_LIMIT:,} entries had to be looked at"))
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in LEFT_OUT or os.path.exists(os.path.join(entry.path, "CACHEDIR.TAG")):
                        continue
                    if os.path.exists(os.path.join(entry.path, ".git")):
                        inner += 1      # a repository: it has its own index, and its own say on what belongs to it
                        continue
                    pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                found = entry.stat(follow_symlinks=False)
                if found.st_size > filters.MAX_BYTES:
                    continue
                path = os.path.relpath(entry.path, root).replace(os.sep, "/")
                stamp = [found.st_size, found.st_mtime_ns, found.st_ctime_ns]
                known = remembered.get(path)
                if known is not None and known[:3] == stamp and found.st_ctime_ns < known[3]:
                    now[path] = known
                else:
                    with open(entry.path, "rb") as file:
                        now[path] = [*stamp, started, gitio.hash_blob(file.read(), "sha1")]
                    changed = True
            except OSError:
                continue    # unreadable, or removed while we looked
            files[path] = now[path][4]
            if len(files) > FILE_LIMIT:
                raise Refused(_too_large(root, f"it has more than {FILE_LIMIT:,} files"))
    if not files and inner:
        raise Refused(f"{root} is not a git repository: it holds {inner} git repositories and no files of its own. "
                      f"Start Aleph inside one of them")
    if changed or now.keys() != remembered.keys():
        _remember(root, kept / MEMORY, now)
    return root, kept, files, inner


def forget() -> None:
    """Drop what this process remembers of the memory files (tests; a session over its memory share)."""
    _REMEMBERED.clear()


def _too_large(root: Path, what: str) -> str:
    return (f"{root} is not inside a git repository and is too large to be searched as one project: {what} "
            f"(not counting {WHAT_IS_LEFT_OUT}). Run `git init` there and let a `.gitignore` say what belongs to it, "
            f"or start Aleph in a smaller directory")


def _stamp(path: Path) -> tuple[int, int]:
    try:
        found = path.stat()
        return found.st_mtime_ns, found.st_size
    except OSError:
        return 0, 0


def _remembered(root: Path, file: Path) -> dict[str, list]:
    stamp = _stamp(file)
    held = _REMEMBERED.get(root)
    if held is not None and held[0] == stamp:
        return held[1]
    try:
        found = json.loads(file.read_text())
        found = found if isinstance(found, dict) and all(isinstance(each, list) and len(each) == 5 for each in found.values()) else {}
    except (OSError, ValueError):
        found = {}      # none yet, or cut short by a crash: everything is hashed again
    _REMEMBERED[root] = (stamp, found)
    return found


def _remember(root: Path, file: Path, now: dict[str, list]) -> None:
    """Replace the memory file whole: written aside and renamed, so that another session reads the old or the new."""
    try:
        aside = file.with_name(f"{file.name}.{os.getpid()}")
        aside.write_text(json.dumps(now, separators=(",", ":")))
        os.replace(aside, file)
    except OSError:
        return          # a memory that cannot be written costs time on the next query, nothing else
    _REMEMBERED[root] = (_stamp(file), now)
