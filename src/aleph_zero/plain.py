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
import re
import sys
import tempfile
import threading
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
_LOCK = threading.Lock()    # the server's call thread and its embedding thread both walk
_BLOB = re.compile(r"^[0-9a-f]{40}$")
_TEMPORARY = ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")
# The top directories of a system. Nothing in the first is a project; in the second, a project lies at least two
# levels down (`/usr/local/src/x`, `/Users/me/x`), so a directory directly in one (`/private/var`, somebody's home,
# a disk under `/Volumes`) is not walked.
_NEVER = frozenset({"proc", "sys", "dev", "System"})
_SYSTEM = frozenset({"private", "var", "etc", "usr", "bin", "sbin", "lib", "lib64", "boot", "run", "opt", "srv",
                     "Library", "Applications", "Volumes", "mnt", "media", "home", "Users"})
_DATA_VOLUME = ("/", "System", "Volumes", "Data")      # macOS: another name for `/`, under which the home directory lies too


class Refused(gitio.NotARepository):
    """A directory without git that is not walked, or was found too large to be one project."""


def refusal(root: Path) -> str | None:
    """Why a (resolved) directory is not walked at all; None when it may be."""
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        home = Path("/")
    if len(home.parts) <= 1:    # HOME is empty: there is nothing to keep out of, so nothing is walked
        return (f"{root} is not inside a git repository, and your home directory is not known (HOME is empty): Aleph "
                f"walks a directory without git only when it can tell that it is not your home. Run `git init` there")
    # The same directory, however its path is spelled: on a file system that takes HOME for home, so must this,
    # and macOS has a second name for the whole disk.
    named = Path("/", *root.parts[4:]) if root.parts[:4] == _DATA_VOLUME else root
    if any(each in (root, named) or _same(root, each) for each in (home, *home.parents)):
        return (f"{root} is not inside a git repository, and it is your home directory or holds it: Aleph does not walk "
                f"that. Start it in a project's directory, or run `git init` in one")
    temporary = {Path(tempfile.gettempdir()).resolve(), *(Path(each).resolve() for each in _TEMPORARY)}
    top = named.parts[1] if len(named.parts) > 1 else ""
    if (len(named.parts) <= 2 or top in _NEVER or (top in _SYSTEM and len(named.parts) <= 3)
            or os.path.ismount(root) or root in temporary):
        return (f"{root} is not inside a git repository, and it is the top of a disk, a system directory or the "
                f"temporary directory: Aleph does not walk that. Start it in a project's directory")
    return None


def _same(one: Path, other: Path) -> bool:
    try:
        return os.path.samefile(one, other)
    except OSError:
        return False


def _canonical(directory: Path) -> Path:
    """The directory's path as the file system spells it: `…/HOME` and `…/home` are one directory where case
    does not count, and must be one refusal and one index."""
    root = directory.resolve()
    if sys.platform == "darwin":
        import fcntl
        try:
            opened = os.open(root, os.O_RDONLY)
            try:
                root = Path(os.fsdecode(fcntl.fcntl(opened, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0]))
            finally:
                os.close(opened)
        except OSError:
            pass
    return root


def read(directory: Path) -> tuple[Path, Path, dict[str, str], int, int]:
    """(the directory, where its state is kept, path -> blob id of every file searched, repositories inside
    that were not descended into, files that could not be read). Raises Refused where the rule says so, before
    or while walking."""
    root = _canonical(directory)
    unreadable_repository(directory)
    why = refusal(root)
    if why:
        raise Refused(why)
    try:
        kept = store_db.cache_dir_for(root)
    except OSError as exc:
        raise store_db.StoreError(f"Aleph cannot keep an index for {root}: its cache directory cannot be written ({exc}). "
                                  f"It writes nothing into a directory that is not a git repository: point XDG_CACHE_HOME "
                                  f"at a directory it may use") from exc
    remembered = _remembered(root, kept / MEMORY)
    now: dict[str, list] = {}
    try:
        files, inner, unread = _walk(root, remembered, now)
    except (gitio.GitTimeout, Refused):
        if now:     # out of time, or too large: what was hashed is kept, so the next attempt does not start over
            _remember(root, kept / MEMORY, {**remembered, **now})
        else:
            _leave_nothing(kept)
        raise
    if not files and inner:
        _leave_nothing(kept)
        raise Refused(f"{root} is not a git repository: it holds {inner} git repositories and no files of its own. "
                      f"Start Aleph inside one of them")
    if now != remembered:
        _remember(root, kept / MEMORY, now)
    return root, kept, files, inner, unread


def _device(entry: os.DirEntry) -> int:
    return entry.stat(follow_symlinks=False).st_dev


def unreadable_repository(directory: Path) -> None:
    """Raise GitError when git said "not a repository" of a directory that has a `.git` at or above it: a damaged
    `.git`, or GIT_CEILING_DIRECTORIES in the environment (GIT_DIR is not passed on to git: ADR-014). It is git's to read, not a plain directory:
    walking it would search what its `.gitignore` keeps out."""
    root = _canonical(directory)
    written = Path(os.path.abspath(directory))      # as it was named: a link inside a repository is inside it
    for above in dict.fromkeys((root, *root.parents, written, *written.parents)):
        if os.path.lexists(above / ".git"):
            raise gitio.GitError(f"git cannot read the repository at {above}: it says {root} is in no repository, though "
                                 f"a `.git` is there. Repair it (or look at GIT_CEILING_DIRECTORIES in the "
                                 f"environment); Aleph does not search a repository as if it were a plain directory")


def _leave_nothing(kept: Path) -> None:
    """A directory that was refused before anything of it was remembered gets no folder in the cache."""
    try:
        (kept / "repository").unlink(missing_ok=True)
        kept.rmdir()
    except OSError:
        pass        # something is in it after all (an index from before the directory grew): it stays


def _walk(root: Path, remembered: dict[str, list], now: dict[str, list]) -> tuple[dict[str, str], int, int]:
    """(path -> blob id, repositories inside, files that could not be read). `now` is filled as it goes: what
    is remembered of each file seen."""
    started = time.time_ns()
    files: dict[str, str] = {}
    inner = looked = read = unread = 0
    try:
        found = os.stat(Path.home())
        home = (found.st_dev, found.st_ino)
    except (OSError, RuntimeError):
        home = None
    disk = os.stat(root).st_dev
    pending = [root]
    while pending:
        here = pending.pop()
        entries = []
        try:
            with os.scandir(here) as listing:
                for entry in listing:       # counted while listing: one directory of a million entries is not read through
                    looked += 1
                    if looked % 64 == 0:
                        gitio.check_deadline()
                    if looked > ENTRY_LIMIT:
                        raise Refused(_too_large(root, f"more than {ENTRY_LIMIT:,} entries had to be looked at"))
                    entries.append(entry)
        except OSError:
            continue        # unreadable, or gone while we looked
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            read += 1
            if read % 64 == 0:
                gitio.check_deadline()      # hashing is where the time goes: a caller's limit holds here too
            if entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() in LEFT_OUT or os.path.exists(os.path.join(entry.path, "CACHEDIR.TAG")):
                        continue
                    if os.path.lexists(os.path.join(entry.path, ".git")):     # also one that points nowhere any more
                        inner += 1      # a repository: it has its own index, and its own say on what belongs to it
                        continue
                    # Whatever path led here: the home directory is not read, and neither is another disk mounted inside.
                    if home is not None and (_device(entry), entry.inode()) == home:
                        raise Refused(f"{root} is not inside a git repository and holds your home directory "
                                      f"({entry.path}): Aleph does not walk that. Start it in a project's directory")
                    if _device(entry) != disk:
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
                    try:
                        with open(entry.path, "rb") as file:
                            now[path] = [*stamp, started, gitio.hash_blob(file.read(), "sha1")]
                    except PermissionError:
                        unread += 1     # there, and not ours to read: said in `status`, like a repository's
                        continue
            except OSError:
                continue    # removed while we looked
            files[path] = now[path][4]
            if len(files) > FILE_LIMIT:
                raise Refused(_too_large(root, f"it has more than {FILE_LIMIT:,} files"))
    return files, inner, unread


def forget() -> None:
    """Drop what this process remembers of the memory files (tests; a session over its memory share)."""
    with _LOCK:
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


def _believable(path: object, entry: object) -> bool:
    """Size, two times, the walk's time and a blob id: anything else in the file is not remembered at all."""
    return (isinstance(path, str) and isinstance(entry, list) and len(entry) == 5
            and all(type(number) is int for number in entry[:4]) and isinstance(entry[4], str) and bool(_BLOB.match(entry[4])))


def _remembered(root: Path, file: Path) -> dict[str, list]:
    stamp = _stamp(file)
    with _LOCK:
        held = _REMEMBERED.get(root)
    if held is not None and held[0] == stamp:
        return held[1]
    try:
        found = json.loads(file.read_text())
        found = {path: entry for path, entry in found.items() if _believable(path, entry)} if isinstance(found, dict) else {}
    except (OSError, ValueError):
        found = {}      # none yet, or cut short by a crash: everything is hashed again
    with _LOCK:
        _REMEMBERED[root] = (stamp, found)
    return found


def _remember(root: Path, file: Path, now: dict[str, list]) -> None:
    """Replace the memory file whole: written aside under a name of its own and renamed, so that another
    session, or another thread of this one, reads the old file or the new and never a mixture."""
    aside = None
    try:
        handle, aside = tempfile.mkstemp(dir=file.parent, prefix=file.name + ".", suffix=".part")
        with os.fdopen(handle, "w") as out:
            out.write(json.dumps(now, separators=(",", ":")))
        os.replace(aside, file)
    except OSError:
        if aside is not None:
            try:
                os.unlink(aside)
            except OSError:
                pass
        return          # a memory that cannot be written costs time on the next query, nothing else
    with _LOCK:
        _REMEMBERED[root] = (_stamp(file), dict(now))
