"""The chunk record and its content hash."""
from __future__ import annotations

import hashlib
import math
import textwrap
from dataclasses import dataclass

# Bump when what is extracted from a file changes (chunk boundaries, symbols, imports):
# every blob is then processed again. 2: words are recognised in every script, not only
# ASCII. 3: source code is chunked by definition, with symbols and imports. 4: methods of
# object literals and every target of a chained assignment are definitions. 5: call sites,
# import bindings, base types and field types are read for the code graph; chunks are as before.
CHUNKER_VERSION = 5
# Part of every chunk's hash. A chunk that comes out of a newer chunker with the same kind,
# names and text keeps its hash, and with it its row in the text index and its vector:
# embedding a large repository takes minutes to hours. Bump this only when what is stored
# for the same chunk would differ (how its words are indexed, what text is embedded); then
# everything is indexed and embedded again. It is 3 because the chunker's version was part
# of the hash until that version.
HASH_FORMAT = 3
# Measured in M0 on 35,851 Python functions with the EmbeddingGemma 2 tokenizer.
TOKENS_PER_CHAR = 0.2919


@dataclass(frozen=True)
class Chunk:
    kind: str          # 'doc', 'block' (a window of text), 'module', or a definition's kind
    symbol: str        # the name a reader would search for; empty for a plain window
    signature: str     # one line that introduces the chunk (a heading, later a function signature)
    breadcrumb: str    # where the chunk sits: "Guide > Install", later "Class > method"
    doc: str           # prose attached to the chunk, if any
    text: str          # the chunk's source text, exactly as in the file
    start_line: int    # 1-based, inclusive
    end_line: int      # 1-based, inclusive
    # The whole definition this chunk belongs to, when it is one piece of a long function
    # or the part of a class its methods do not cover. 0 when the chunk stands alone.
    scope_start_line: int = 0
    scope_end_line: int = 0


def normalize(text: str) -> str:
    """Text as hashed: line endings, trailing blanks and common indentation do not matter."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return textwrap.dedent("\n".join(lines)).strip("\n")


def chunk_hash(family: str, chunk: Chunk) -> str:
    """Identity of a chunk. Paths and line numbers are not part of it, so a chunk that
    moves, or whose file is renamed, keeps its hash and everything derived from it."""
    payload = "\0".join((str(HASH_FORMAT), family, chunk.kind, chunk.breadcrumb, chunk.symbol,
                         chunk.signature, normalize(chunk.text)))
    return hashlib.sha256(payload.encode("utf-8", "surrogateescape")).hexdigest()


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) * TOKENS_PER_CHAR)
