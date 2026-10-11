"""The chunk record and its content hash."""
from __future__ import annotations

import hashlib
import math
import textwrap
from dataclasses import dataclass

# Bump when chunk boundaries or hashing change: every blob is then re-chunked.
CHUNKER_VERSION = 1
# Measured in M0 on 35,851 Python functions with the EmbeddingGemma 2 tokenizer.
TOKENS_PER_CHAR = 0.2919


@dataclass(frozen=True)
class Chunk:
    kind: str          # 'doc' for documentation, 'block' for a window of any other text
    symbol: str        # the name a reader would search for; empty for a plain window
    signature: str     # one line that introduces the chunk (a heading, later a function signature)
    breadcrumb: str    # where the chunk sits: "Guide > Install", later "Class > method"
    doc: str           # prose attached to the chunk, if any
    text: str          # the chunk's source text, exactly as in the file
    start_line: int    # 1-based, inclusive
    end_line: int      # 1-based, inclusive


def normalize(text: str) -> str:
    """Text as hashed: line endings, trailing blanks and common indentation do not matter."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return textwrap.dedent("\n".join(lines)).strip("\n")


def chunk_hash(family: str, chunk: Chunk) -> str:
    """Identity of a chunk. Paths and line numbers are not part of it, so a chunk that
    moves, or whose file is renamed, keeps its hash and everything derived from it."""
    payload = "\0".join((str(CHUNKER_VERSION), family, chunk.kind, chunk.breadcrumb, normalize(chunk.text)))
    return hashlib.sha256(payload.encode("utf-8", "surrogateescape")).hexdigest()


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) * TOKENS_PER_CHAR)
