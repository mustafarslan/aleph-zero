"""Git plumbing: where the repository is, what a tree holds, what differs on disk."""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

# Never fetch missing objects one at a time from a partial clone, never take
# optional locks from a background reader, and keep git's messages in English.
_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
_REGULAR_MODES = {b"100644", b"100755"}


class GitError(RuntimeError):
    """A git command failed, or the directory is not inside a repository."""


class NotARepository(GitError):
    """The directory is not inside a git repository. Aleph searches what git tracks."""


class NoRepository(NotARepository):
    """Git itself says the directory is in no repository (and not: git is missing, or cannot read one)."""


class GitTimeout(GitError):
    """Work on the repository ran past the caller's deadline."""


def _env() -> dict[str, str]:
    return {**os.environ, **_ENV}


_DEADLINE: ContextVar[float | None] = ContextVar("aleph_git_deadline", default=None)


@contextmanager
def until(deadline: float | None):
    """Inside this block every git call must finish by `deadline` (a time.monotonic()
    value) or raise GitError. A caller that stops waiting also stops the work."""
    token = _DEADLINE.set(deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def _run(command: list[str]) -> subprocess.CompletedProcess:
    deadline = _DEADLINE.get()
    timeout = None if deadline is None else max(deadline - time.monotonic(), 0.001)
    try:
        return subprocess.run(command, capture_output=True, env=_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        raise GitTimeout("git timed out before the time limit") from None


def check_deadline() -> None:
    """Raise GitTimeout if the deadline set by `until` has passed. Called from loops that
    read the working tree, which git's own timeout does not cover."""
    deadline = _DEADLINE.get()
    if deadline is not None and time.monotonic() > deadline:
        raise GitTimeout("reading the worktree timed out before the time limit")


def git(cwd: Path, *args: str) -> bytes:
    proc = _run(["git", "-C", str(cwd), *args])
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[:300]
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {detail}")
    return proc.stdout


@dataclass(frozen=True)
class RepoInfo:
    root: Path            # top of this worktree
    object_format: str    # 'sha1' or 'sha256'
    common_dir: Path      # shared by every worktree of the repository
    tree: str | None      # HEAD's tree, or None before the first commit
    git_dir: Path | None = None  # this worktree's own git directory (its index, its sparse-checkout file)
    plain: bool = False          # not a repository at all: a directory read as it is on disk (plain.py)


def repo_info(cwd: Path) -> RepoInfo:
    """Everything a view needs to know about the repository, in one git process.
    Four separate calls cost about 17 ms; a query pays this every time."""
    proc = _run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel", "--show-object-format",
         "--path-format=absolute", "--git-common-dir", "--git-dir", "--verify", "--quiet", "HEAD^{tree}"])
    lines = proc.stdout.decode().splitlines()
    if len(lines) < 4:  # with no commits yet git prints four lines and exits 1
        detail = proc.stderr.decode(errors="replace").strip()[:300]
        if not Path(cwd).is_dir():
            raise NotARepository(f"{cwd} does not exist or is not a directory")
        if "not a git repository" in detail:
            raise NoRepository(
                f"{cwd} is not inside a git repository. Aleph searches what git tracks: run `git init` there "
                f"(and commit or just add files), or start it inside a repository.")
        raise GitError(f"git could not describe the repository at {cwd}: {detail}")
    return RepoInfo(Path(lines[0]), lines[1], Path(lines[2]), lines[4] if len(lines) > 4 else None, Path(lines[3]))


def toplevel(cwd: Path) -> Path:
    return Path(git(cwd, "rev-parse", "--show-toplevel").decode().strip())


def common_dir(cwd: Path) -> Path:
    """The directory every worktree of a repository shares."""
    return Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())


def object_format(cwd: Path) -> str:
    return git(cwd, "rev-parse", "--show-object-format").decode().strip()


def head_tree(cwd: Path) -> str | None:
    """Tree id of HEAD, or None in a repository with no commits yet."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--verify", "--quiet", "HEAD^{tree}"],
        capture_output=True, env=_env(),
    )
    return proc.stdout.decode().strip() or None


def read_tree(cwd: Path, tree: str) -> tuple[dict[str, str], tuple[str, ...]]:
    """A tree's regular files as path -> blob id, and the paths of its submodules.
    Symlinks are left out."""
    files: dict[str, str] = {}
    links: list[str] = []
    for entry in git(cwd, "ls-tree", "-r", "-z", "--full-tree", tree).split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode, kind, oid = meta.split(b" ")
        if kind == b"blob" and mode in _REGULAR_MODES:
            files[path.decode("utf-8", "surrogateescape")] = oid.decode()
        elif kind == b"commit":
            links.append(path.decode("utf-8", "surrogateescape"))
    return files, tuple(links)


def ls_tree(cwd: Path, tree: str) -> dict[str, str]:
    """path -> blob id for every regular file in a tree. Symlinks and submodules are left out."""
    return read_tree(cwd, tree)[0]


def skip_worktree(cwd: Path) -> tuple[frozenset[str], tuple[str, ...]]:
    """What git tracks but has not checked out here (a sparse checkout): the files, and,
    where git's index keeps whole directories collapsed, those directories with a trailing
    slash. Only asked in a worktree that has a sparse-checkout file."""
    files: set[str] = set()
    directories: list[str] = []
    for entry in git(cwd, "ls-files", "-t", "-z", "--sparse").split(b"\0"):
        if entry[:2] != b"S ":
            continue
        path = entry[2:].decode("utf-8", "surrogateescape")
        if path.endswith("/"):
            directories.append(path)
        else:
            files.add(path)
    return frozenset(files), tuple(directories)


def status(cwd: Path) -> list[str]:
    """Every path that differs from HEAD or is untracked. A rename reports both names.

    Untracked files are asked for in git's `normal` form, where a wholly untracked
    directory is reported as one entry, and such directories are then listed by a
    second call. The `all` form gives the same files in one call, but git's untracked
    cache cannot answer it: with `core.untrackedCache` on, this form took 20 ms on
    Django against 66 ms (ADR-008)."""
    tokens = git(cwd, "status", "--porcelain=v2", "-z", "--untracked-files=normal").split(b"\0")
    paths: list[bytes] = []
    directories: list[bytes] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        tag = token[:2]
        if tag == b"1 ":
            paths.append(token.split(b" ", 8)[8])
        elif tag == b"2 ":
            paths.append(token.split(b" ", 9)[9])
            paths.append(tokens[index])  # the name it was renamed or copied from
            index += 1
        elif tag == b"u ":
            paths.append(token.split(b" ", 10)[10])
        elif tag == b"? ":
            (directories if token.endswith(b"/") else paths).append(token[2:])
    for batch in _batches(directories):
        # Literal: a directory may be named `:(top)x` or `-n`, which git would otherwise
        # read as pathspec magic or an option and silently list nothing for.
        listed = _git_bytes(cwd, [b"--literal-pathspecs", b"ls-files", b"--others", b"--exclude-standard", b"-z",
                                  b"--", *batch])
        paths.extend(path for path in listed.split(b"\0") if path)
    return [path.decode("utf-8", "surrogateescape") for path in paths]


class Readers:
    """Blob readers, one per repository, opened when first needed. A view can hold files
    of several repositories: its own and the linked sources."""

    def __init__(self) -> None:
        self._open: dict[Path, BlobReader] = {}

    def reader(self, root: Path) -> "BlobReader":
        found = self._open.get(root)
        if found is None:
            found = self._open[root] = BlobReader(root)
        return found

    def close(self) -> None:
        for reader in self._open.values():
            reader.close()
        self._open.clear()

    def __enter__(self) -> "Readers":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _batches(items: list[bytes], limit: int = 100_000) -> list[list[bytes]]:
    """Split arguments so that no command line exceeds what the system accepts."""
    batches: list[list[bytes]] = []
    size = 0
    for item in items:
        if not batches or size + len(item) + 1 > limit:
            batches.append([])
            size = 0
        batches[-1].append(item)
        size += len(item) + 1
    return batches


def _git_bytes(cwd: Path, args: list[bytes]) -> bytes:
    """Like `git`, for arguments that are paths and may not be valid text."""
    proc = _run([b"git", b"-C", os.fsencode(cwd), *args])
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()[:300]
        raise GitError(f"git {args[1].decode()} failed in {cwd}: {detail}")
    return proc.stdout


def untracked_cache_enabled(cwd: Path) -> bool:
    """True if git keeps its untracked cache for this repository."""
    try:
        return git(cwd, "config", "--bool", "--get", "core.untrackedCache").strip() == b"true"
    except GitError:
        return False


def hash_blob(data: bytes, fmt: str = "sha1") -> str:
    """The id git would give this content as a blob."""
    digest = hashlib.new(fmt)
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


@dataclass(frozen=True)
class TooLarge:
    """A blob that was not read because it is over the caller's limit."""
    size: int


class BlobReader:
    """Reads blobs by id through one long-lived `git cat-file --batch`, with no checkout.
    Git starts on the first read, so a reader that is never used costs nothing."""

    def __init__(self, cwd: Path) -> None:
        self._cwd = cwd
        self._proc: subprocess.Popen | None = None

    def read(self, oid: str, max_bytes: int | None = None) -> bytes | TooLarge | None:
        """The blob's content, or None if the repository does not have it. A blob over
        `max_bytes` is passed over in small steps and reported as TooLarge: someone's
        committed 100 MB database must not be held in memory just to be skipped."""
        if self._proc is None:
            self._proc = subprocess.Popen(
                ["git", "-C", str(self._cwd), "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env(),
            )
        try:
            self._proc.stdin.write(oid.encode() + b"\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
        except OSError:
            line = b""
        if not line:  # git is gone: that says nothing about whether the blob exists
            detail = self._proc.stderr.read().decode(errors="replace").strip()[:300]
            raise GitError(f"git cat-file stopped in {self._cwd}: {detail}")
        header = line.split()
        if len(header) != 3:  # "<oid> missing"
            return None
        size = int(header[2])
        if max_bytes is not None and size > max_bytes:
            left = size + 1  # the content and the newline after it
            while left > 0:
                left -= len(self._proc.stdout.read(min(left, 1 << 16)))
            return TooLarge(size)
        data = self._proc.stdout.read(size)
        self._proc.stdout.read(1)  # the newline after the content
        return data

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def __enter__(self) -> "BlobReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
