"""Giving the chunks of a view their vectors, in the background.

This is slow work (about a hundred chunks a second on the GPU: twenty minutes for a
7,000-file repository), so it is never on a query's path. It can be stopped at any
batch and picks up where it left off, because a chunk either has its vector in the
store or it does not. One session at a time embeds for a repository; the others
leave it to that one and use the vectors as they appear.
"""
from __future__ import annotations

import fcntl
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .embed.client import EmbedClient, Unavailable
from .indexer import Index
from .vectors import VectorStore

BATCH = 32


@dataclass(frozen=True)
class EmbedReport:
    embedded: int
    remaining: int    # chunks of the view still without a vector
    seconds: float
    reason: str = ""  # why work stopped before the end, if it did not just run out of time


def chunk_text(breadcrumb: str, signature: str, text: str) -> str:
    """What is embedded for a chunk: where it sits, its signature, its text. Not its path:
    a chunk is content, shared by every file and branch that holds it (ADR-001)."""
    lines = [breadcrumb] if breadcrumb else []
    if signature and signature != breadcrumb and signature.strip("# ") != breadcrumb.rsplit(" > ", 1)[-1]:
        lines.append(signature)
    return "\n".join([*lines, text])


@contextmanager
def embedding_lock(directory: Path):
    """Held by the one session that embeds for this repository. Yields False, at once,
    if another session holds it."""
    handle = open(Path(directory) / "embed.lock", "w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError:
            held = False
        yield held
    finally:
        if held:
            fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def embed_pending(index: Index, store: VectorStore, client: EmbedClient, budget_seconds: float = float("inf"),
                  batch: int = BATCH, stop: Callable[[], bool] | None = None,
                  progress: Callable[[int, int], None] | None = None) -> EmbedReport:
    """Embed chunks of the index's current view that have no vector, for at most
    `budget_seconds` (at least one batch) or until `stop()`. Never raises for a worker
    that is unavailable: the report says why nothing more was done."""
    started = time.monotonic()
    embedded, reason = 0, ""
    with embedding_lock(store.path.parent) as held:
        if not held:
            reason = "another session is embedding this repository"
        while held and not (stop is not None and stop()):
            wanted = store.missing(index, limit=batch)
            if not wanted:
                break
            rows = index.conn.execute(
                f"SELECT chunk_id, breadcrumb, signature, text FROM chunks WHERE chunk_id IN ({','.join('?' * len(wanted))}) "
                "ORDER BY chunk_id", wanted).fetchall()
            if not rows:
                break  # collected by another session between the two statements
            try:
                matrix = client.embed([chunk_text(*row[1:]) for row in rows], kind="document")
            except Unavailable as exc:
                reason = str(exc)
                break
            stored = store.add([row[0] for row in rows], matrix)
            embedded += stored
            if progress is not None:
                progress(embedded, len(wanted))
            if stored == 0 and len(rows) < len(wanted):
                break  # nothing left that still exists
            if time.monotonic() - started >= budget_seconds:
                break
    have, total = store.coverage(index)
    return EmbedReport(embedded, total - have, time.monotonic() - started, reason)
