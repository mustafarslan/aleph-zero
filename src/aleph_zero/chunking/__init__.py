"""Turning a file's text into content-addressed chunks."""
from .fallback import chunk_file, markdown_chunks, window_chunks
from .model import CHUNKER_VERSION, Chunk, chunk_hash, estimate_tokens, normalize

__all__ = [
    "CHUNKER_VERSION", "Chunk", "chunk_file", "chunk_hash", "estimate_tokens",
    "markdown_chunks", "normalize", "window_chunks",
]
