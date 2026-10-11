"""Git plumbing: where the repository is, what a tree holds, what differs on disk."""
from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Never fetch missing objects one at a time from a partial clone, never take
# optional locks from a background reader, and keep git's messages in English.
_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
_REGULAR_MODES = {b"100644", b"100755"}


class GitError(RuntimeError):
    """A git command failed, or the directory is not inside a repository."""


def _env() -> dict[str, str]:
    return {**os.environ, **_ENV}


def git(cwd: Path, *args: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, env=_env())
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


def repo_info(cwd: Path) -> RepoInfo:
    """Everything a view needs to know about the repository, in one git process.
    Four separate calls cost about 17 ms; a query pays this every time."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel", "--show-object-format",
         "--path-format=absolute", "--git-common-dir", "--verify", "--quiet", "HEAD^{tree}"],
        capture_output=True, env=_env(),
    )
    lines = proc.stdout.decode().splitlines()
    if len(lines) < 3:  # with no commits yet git prints three lines and exits 1
        detail = proc.stderr.decode(errors="replace").strip()[:300]
        raise GitError(f"not a git repository: {cwd}: {detail}")
    return RepoInfo(Path(lines[0]), lines[1], Path(lines[2]), lines[3] if len(lines) > 3 else None)


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


def ls_tree(cwd: Path, tree: str) -> dict[str, str]:
    """path -> blob id for every regular file in a tree. Symlinks and submodules are left out."""
    files: dict[str, str] = {}
    for entry in git(cwd, "ls-tree", "-r", "-z", "--full-tree", tree).split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        mode, kind, oid = meta.split(b" ")
        if kind == b"blob" and mode in _REGULAR_MODES:
            files[path.decode("utf-8", "surrogateescape")] = oid.decode()
    return files


def status(cwd: Path) -> list[str]:
    """Every path that differs from HEAD or is untracked. A rename reports both names."""
    tokens = git(cwd, "status", "--porcelain=v2", "-z", "--untracked-files=all").split(b"\0")
    paths: list[bytes] = []
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
            paths.append(token[2:])
    return [path.decode("utf-8", "surrogateescape") for path in paths]


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
