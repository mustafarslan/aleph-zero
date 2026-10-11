"""Turning a file's text into content-addressed chunks."""
from .code import SPLIT_TOKENS, CodeFile, code_chunks
from .fallback import chunk_file, markdown_chunks, window_chunks
from .model import CHUNKER_VERSION, Chunk, chunk_hash, estimate_tokens, normalize

__all__ = [
    "CHUNKER_VERSION", "SPLIT_TOKENS", "Chunk", "CodeFile", "chunk_file", "chunk_hash", "code_chunks",
    "estimate_tokens", "markdown_chunks", "normalize", "window_chunks",
]
