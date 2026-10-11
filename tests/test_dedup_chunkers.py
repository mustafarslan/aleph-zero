import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "spikes"))

import dedup  # noqa: E402

BEFORE = b"""import os

LIMIT = 3


def first():
    return 1


def second():
    return 2
"""
AFTER = b"import os\nimport sys\n" + BEFORE[len(b"import os\n"):]


def test_ast_chunks_are_one_per_definition_plus_module_level_code():
    chunks = dedup.ast_chunks(BEFORE)
    assert len(chunks) == 3
    assert chunks[0].startswith("import os")
    assert chunks[1].startswith("def first")
    assert chunks[2].startswith("def second")


def test_added_import_invalidates_one_ast_chunk_but_every_window():
    ast_before, ast_after = set(dedup.ast_chunks(BEFORE)), set(dedup.ast_chunks(AFTER))
    assert len(ast_after - ast_before) == 1
    padded_before = BEFORE + b"\n".join(b"x = %d" % i for i in range(120))
    padded_after = AFTER + b"\n".join(b"x = %d" % i for i in range(120))
    win_before, win_after = set(dedup.window_chunks(padded_before)), set(dedup.window_chunks(padded_after))
    assert len(win_after - win_before) == len(win_after)


def test_large_class_is_split_into_methods():
    body = "".join(f"    def method_{i}(self):\n        return {i}\n\n" for i in range(45))
    chunks = dedup.ast_chunks(("class Big:\n" + body).encode())
    assert len(chunks) == 46  # header + 45 methods
    assert chunks[0].startswith("class Big:")
