"""Which files are worth indexing, and as what."""
from __future__ import annotations

from pathlib import PurePosixPath

from . import lang

MAX_BYTES = 300_000
MAX_AVERAGE_LINE = 500
MARKDOWN_SUFFIXES = {".md", ".mdx"}
PROSE_SUFFIXES = {".rst", ".txt"}
DOC_FAMILIES = {"markdown", "prose"}
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock", "poetry.lock",
    "uv.lock", "go.sum", "composer.lock", "Gemfile.lock",
}
_LFS_POINTER = b"version https://git-lfs.github.com/spec/"


def family(path: str) -> str:
    """The treatment a path selects. The index holds one set of chunks per (blob, family),
    so the same bytes at `notes.md` and `notes.py` are each chunked their own way."""
    pure = PurePosixPath(path)
    if pure.name in LOCKFILES:
        return "lockfile"
    suffix = pure.suffix.lower()
    if suffix in MARKDOWN_SUFFIXES:
        return "markdown"
    if suffix in PROSE_SUFFIXES:
        return "prose"
    profile = lang.profile_for_path(path)
    return profile.name if profile else "text"


def classify(family: str, data: bytes) -> tuple[str, str | None]:
    """('doc' | 'text', decoded text) for an indexable file, or ('skip:<reason>', None)."""
    if family == "lockfile":
        return "skip:lockfile", None
    if len(data) > MAX_BYTES:
        return "skip:too-large", None
    if data.startswith(_LFS_POINTER):
        return "skip:lfs-pointer", None
    if b"\0" in data[:8192]:
        return "skip:binary", None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "skip:not-utf8", None
    if not text.strip():
        return "skip:empty", None
    lines = text.count("\n") + 1
    if len(text) / lines > MAX_AVERAGE_LINE:
        return "skip:minified", None
    return ("doc" if family in DOC_FAMILIES else "text"), text
