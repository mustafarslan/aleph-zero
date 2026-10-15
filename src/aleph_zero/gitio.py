"""Git plumbing: where the repository is, what a tree holds, what differs on disk."""
from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

# Never fetch missing objects one at a time from a partial clone, never take
# optional locks from a background reader, and keep git's messages in English.
_ENV = {"GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
_REGULAR_MODES = {b"100644", b"100755"}


class GitError(RuntimeError):
    """A git command failed, or the directory is not inside a repository."""


class NotARepository(GitError):
    """The directory is not inside a git repository. Aleph searches what git tracks."""


class NoRepository(NotARepository):
    """Git itself says the directory is in no repository (and not: git is missing, or cannot read one)."""


class GitTimeout(GitError):
    """Work on the repository ran past the caller's deadline."""


_UNATTENDED: ContextVar[bool] = ContextVar("aleph_git_unattended", default=False)


@contextmanager
def unattended():
    """Inside this block git is run for a fetch nobody watches (a hook, a schedule): it never asks for a name or a
    password on the terminal, and options given to the command that started the hook (`git -c ... pull`) are
    not carried to another repository's remote."""
    token = _UNATTENDED.set(True)
    try:
        yield
    finally:
        _UNATTENDED.reset(token)


# What says WHICH repository a git command works on, whatever directory it is run in. In the environment they
# belong to the repository Aleph was pointed at: a repository used through `git --git-dir=… --work-tree=…` has
# no `.git` to be found and is a repository only by them. A command meant for ANOTHER repository (a linked
# source's clone, a source linked by path) must not carry them: `git -C <clone> fetch` with GIT_DIR set fetches
# into the repository GIT_DIR names, and with `--prune +refs/heads/*:refs/heads/*` that replaced and deleted
# its branches.
_WHICH_REPOSITORY = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX")
# Options of the command that started us (`git -c … pull` and its hook), not the user's own configuration
# files (GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM): those stay.
_CALLERS_OPTIONS = re.compile(r"GIT_CONFIG_(PARAMETERS|COUNT|KEY_\d+|VALUE_\d+)\Z")
_ELSEWHERE: ContextVar[bool] = ContextVar("aleph_git_elsewhere", default=False)


@contextmanager
def elsewhere():
    """Inside this block git is run for a repository other than the one Aleph was pointed at: what the
    environment says about which repository is meant is not passed on, so `-C` decides."""
    token = _ELSEWHERE.set(True)
    try:
        yield
    finally:
        _ELSEWHERE.reset(token)


def _env() -> dict[str, str]:
    env = {**os.environ, **_ENV}
    if _ELSEWHERE.get():
        for name in _WHICH_REPOSITORY:
            env.pop(name, None)
    if _UNATTENDED.get():
        for name in [name for name in env if _CALLERS_OPTIONS.match(name)]:
            del env[name]
        # Nobody is there to answer: no question on the terminal, none in a window, none from ssh.
        env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "SSH_ASKPASS": "true", "GCM_INTERACTIVE": "never"})
        env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    return env


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


def _start(command: list[str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env())
    except FileNotFoundError:
        raise GitError("git is not installed, or not on the PATH: Aleph asks git what a repository holds, and whether "
                       "a directory is in one. Install git and try again") from None


def _run(command: list[str]) -> subprocess.CompletedProcess:
    return _finish(_start(command), command)


def _finish(proc: subprocess.Popen, command: list[str]) -> subprocess.CompletedProcess:
    """Wait for a git process that was started, by the deadline in force now."""
    deadline = _DEADLINE.get()
    timeout = None if deadline is None else max(deadline - time.monotonic(), 0.001)
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(command, proc.returncode, out, err)
    except subprocess.TimeoutExpired:
        # git starts helpers for the network (git-remote-https, ssh). Killing git alone left them running, holding
        # a connection to a server that never answers: one pair for every fetch that ran out of time.
        _stop_with_children(proc)
        raise GitTimeout("git timed out before the time limit") from None
    except BaseException:
        _stop_with_children(proc)       # interrupted: nothing of it is left behind either
        raise


def _children(pid: int) -> list[int]:
    """The processes a process started, and theirs. Asked of the system while they still run."""
    try:
        found = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    direct = [int(child) for child in found if child.isdigit()]
    return direct + [below for child in direct for below in _children(child)]


def _stop_with_children(proc: subprocess.Popen) -> None:
    # Not a process group of git's own: it would take git out of the terminal's foreground, and a person
    # adding a source is then never asked for the password git wants to ask for.
    for pid in [*_children(proc.pid), proc.pid]:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


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


_STATUS = ("status", "--porcelain=v2", "-z", "--untracked-files=normal")


class StatusAhead:
    """`git status`, started and not yet waited for: so that it runs while git is asked for the repository's facts
    (9 to 11 ms of every question, when the two came one after the other). It may be started anywhere in the
    worktree: git gives its paths from the top. Whoever starts one ends it, with `paths` or with `forget`."""

    def __init__(self, cwd: Path) -> None:
        self._command = ["git", "-C", str(cwd), *_STATUS]
        self._cwd = cwd
        self._proc: subprocess.Popen | None = _start(self._command)

    def paths(self, root: Path) -> list[str]:
        """What `status(root)` gives. `root` is the top of the worktree: what git lists afterwards is asked for there."""
        proc, self._proc = self._proc, None
        if proc is None:
            raise GitError("this status was read or given up already")
        done = _finish(proc, self._command)
        if done.returncode != 0:
            detail = done.stderr.decode(errors="replace").strip()[:300]
            raise GitError(f"git {' '.join(_STATUS)} failed in {self._cwd}: {detail}")
        return _status_paths(root, done.stdout)

    def forget(self) -> None:
        """Not wanted after all (no repository there, or something else failed first): nothing of it is left running."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        if proc.poll() is None:
            _stop_with_children(proc)
        else:                           # it has ended by itself (no repository there): only its pipes are left to close
            proc.communicate()


def status(cwd: Path) -> list[str]:
    """Every path that differs from HEAD or is untracked. A rename reports both names.

    Untracked files are asked for in git's `normal` form, where a wholly untracked
    directory is reported as one entry, and such directories are then listed by a
    second call. The `all` form gives the same files in one call, but git's untracked
    cache cannot answer it: with `core.untrackedCache` on, this form took 20 ms on
    Django against 66 ms (ADR-008)."""
    return _status_paths(cwd, git(cwd, *_STATUS))


def _status_paths(cwd: Path, answer: bytes) -> list[str]:
    tokens = answer.split(b"\0")
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

    def reader(self, root: Path, other: bool = False) -> "BlobReader":
        """The reader of the repository at `root`; `other` for a repository that is not the one Aleph was
        pointed at (a linked source), whose git must not be told by the environment which repository it is in."""
        found = self._open.get(root)
        if found is None:
            found = self._open[root] = BlobReader(root, other)
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
    """True if git keeps its untracked cache for this repository: because its configuration says so, or because
    the cache is in the repository's index (`git update-index --untracked-cache` puts it there and changes no
    configuration; git then keeps and uses it)."""
    try:
        if git(cwd, "config", "--bool", "--get", "core.untrackedCache").strip() == b"true":
            return True
    except GitError:
        pass
    try:
        index = Path(git(cwd, "rev-parse", "--path-format=absolute", "--git-dir").decode().strip()) / "index"
        return _has_extension(index.read_bytes(), b"UNTR")
    except (GitError, OSError):
        return False


def _has_extension(index: bytes, signature: bytes) -> bool:
    """Whether a git index file carries an extension. Extensions follow the entries, each a four-letter name
    and a length, up to the checksum at the end; a name found by chance inside an entry does not lead there."""
    for checksum in (20, 32):
        end = len(index) - checksum
        at = index.find(signature)
        while at != -1:
            here = at
            while here + 8 <= end and index[here:here + 4].isalpha():
                here += 8 + int.from_bytes(index[here + 4:here + 8], "big")
                if here == end:
                    return True
            at = index.find(signature, at + 1)
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

    def __init__(self, cwd: Path, other: bool = False) -> None:
        self._cwd = cwd
        self._other = other
        self._proc: subprocess.Popen | None = None

    def read(self, oid: str, max_bytes: int | None = None) -> bytes | TooLarge | None:
        """The blob's content, or None if the repository does not have it. A blob over
        `max_bytes` is passed over in small steps and reported as TooLarge: someone's
        committed 100 MB database must not be held in memory just to be skipped."""
        if self._proc is None:
            with (elsewhere() if self._other else nullcontext()):
                environment = _env()
            self._proc = subprocess.Popen(
                ["git", "-C", str(self._cwd), "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
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
