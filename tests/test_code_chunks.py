import time
from pathlib import Path

import pytest

from aleph_zero import lang
from aleph_zero.chunking import SPLIT_TOKENS, code_chunks, estimate_tokens
from timing import budget  # noqa: E402

SAMPLES = Path(__file__).parent / "samples"
FILES = {"python": "sample.py", "typescript": "sample.ts", "javascript": "sample.js", "go": "sample.go",
         "rust": "sample.rs", "java": "Sample.java", "cpp": "sample.cpp", "c": "sample.c"}


def chunked(name, source=None):
    text = source if source is not None else (SAMPLES / FILES[name]).read_text()
    return code_chunks(lang.PROFILES[name], text)


def by_symbol(chunks, breadcrumb):
    found = [chunk for chunk in chunks if chunk.breadcrumb == breadcrumb]
    assert found, [chunk.breadcrumb for chunk in chunks]
    return found


def test_each_function_and_method_is_a_chunk_of_its_own():
    chunks = chunked("python").chunks
    refresh, = by_symbol(chunks, "refresh_token")
    assert refresh.kind == "function" and refresh.symbol == "refresh_token"
    assert refresh.signature == "def refresh_token(session: Session, *, force: bool = False) -> str"
    assert refresh.doc == "Exchange a refresh token for a new access token."
    assert (refresh.start_line, refresh.end_line) == (10, 12)
    assert refresh.text.startswith("def refresh_token(") and refresh.text.endswith('session.post("/oauth/refresh")')
    size, = by_symbol(chunks, "TokenStore.size")
    assert size.kind == "method" and size.text.startswith("    @property")


def test_a_class_chunk_holds_what_its_methods_do_not():
    chunks = chunked("python").chunks
    store, = by_symbol(chunks, "TokenStore")
    assert store.kind == "class" and store.symbol == "TokenStore" and store.doc == "Keeps tokens."
    assert "class TokenStore(Base):" in store.text and "ttl = 3600" in store.text
    assert "def __init__" not in store.text
    assert (store.scope_start_line, store.scope_end_line) == (15, 30)


def test_what_is_left_of_the_module_is_chunked_too():
    chunks = chunked("python").chunks
    module = [chunk for chunk in chunks if chunk.kind == "module"]
    assert len(module) == 1
    assert "import os" in module[0].text and "MAX_RETRY = 6" in module[0].text
    assert module[0].symbol == "" and module[0].start_line == 1


@pytest.mark.parametrize("name", sorted(FILES))
def test_every_line_with_content_is_in_exactly_one_chunk(name):
    text = (SAMPLES / FILES[name]).read_text()
    covered: dict[int, int] = {}
    for chunk in chunked(name).chunks:
        lines = chunk.text.split("\n")
        assert chunk.start_line >= 1 and chunk.end_line >= chunk.start_line
        for line in lines:
            assert line in text
    result = chunked(name)
    owned = sorted(line for runs in result.runs for first, last in runs for line in range(first, last + 1))
    assert len(owned) == len(set(owned))  # no line twice
    source_lines = text.split("\n")
    with_words = {number for number, line in enumerate(source_lines, 1) if any(ch.isalnum() for ch in line)}
    assert with_words <= set(owned)


def test_chunks_come_in_source_order_and_each_definition_knows_its_chunk():
    result = chunked("go")
    starts = [chunk.start_line for chunk in result.chunks]
    assert starts == sorted(starts)
    where = {d.qualified: result.chunks[ordinal] for d, ordinal in zip(result.definitions, result.chunk_of)}
    assert where["TokenStore.Get"].kind == "method" and where["TokenStore.Get"].symbol == "Get"
    assert where["MaxRetry"].kind == "module"            # a constant lives in the module's chunk
    assert where["TokenStore.Path"].symbol == "TokenStore"  # a field lives in its type's chunk
    assert [item.specifier for item in result.imports] == ["fmt", "strings"]


def test_a_long_function_is_cut_at_line_boundaries_into_pieces_that_share_its_identity():
    body = "".join(f"    total_{i} = compute_value({i}) + offset\n" for i in range(200))
    source = f"def long_one(offset):\n{body}    return offset\n\n\ndef short():\n    return 1\n"
    chunks = chunked("python", source).chunks
    pieces = by_symbol(chunks, "long_one")
    assert len(pieces) > 5
    assert all(estimate_tokens(piece.text) <= SPLIT_TOKENS for piece in pieces)
    assert all(piece.symbol == "long_one" and piece.signature == "def long_one(offset)" for piece in pieces)
    assert {(piece.scope_start_line, piece.scope_end_line) for piece in pieces} == {(1, 202)}
    assert "\n".join(piece.text for piece in pieces) == source.split("\n\n\ndef short")[0]
    assert [piece.start_line for piece in pieces] == sorted(piece.start_line for piece in pieces)
    assert pieces[0].start_line == 1 and pieces[-1].end_line == 202


def test_one_very_long_line_is_kept_whole():
    source = "def wide():\n    data = [" + ", ".join(str(i) for i in range(2000)) + "]\n    return data\n"
    pieces = by_symbol(chunked("python", source).chunks, "wide")
    assert any(estimate_tokens(piece.text) > SPLIT_TOKENS for piece in pieces)  # never truncated
    assert "\n".join(piece.text for piece in pieces) == source.rstrip("\n")


def test_a_twenty_thousand_line_function_is_chunked_quickly():
    source = "def huge():\n" + "".join(f"    value_{i} = {i}\n" for i in range(20_000)) + "    return 0\n"
    started = time.perf_counter()
    chunks = chunked("python", source).chunks
    assert time.perf_counter() - started < budget(1.0)
    assert sum(chunk.text.count("\n") + 1 for chunk in chunks) == 20_002


def test_punctuation_only_leftovers_are_not_chunks():
    source = "namespace a {\n\nint f() { return 1; }\n\n}\n"
    chunks = chunked("cpp", source).chunks
    assert all(any(ch.isalnum() for ch in chunk.text) for chunk in chunks)


def test_source_that_does_not_parse_at_all_still_gets_chunks():
    result = chunked("python", "def broken(:\n    return ((\n" + "x = 1\n" * 30)
    assert result.chunks and "x = 1" in "\n".join(chunk.text for chunk in result.chunks)


def test_two_functions_on_one_line_do_not_claim_the_line_twice():
    result = chunked("rust", "mod inner { pub fn a() {} pub fn b() {} }\n")
    assert sorted(d.qualified for d in result.definitions) == ["inner", "inner.a", "inner.b"]
    assert sum(chunk.text.count("pub fn a") for chunk in result.chunks) == 1


def test_a_module_chunk_never_spans_code_it_does_not_contain():
    functions = "".join(f"def function_{i}():\n    return {i}\n\n\n" for i in range(30))
    source = "import os\n\n\n" + functions + "LATE_CONSTANT = 1\n\n\n" + functions.replace("function_", "other_") + "TAIL = 2\n"
    result = chunked("python", source)
    for chunk, runs in zip(result.chunks, result.runs):
        if chunk.kind == "module":
            assert len(runs) == 1, (chunk.start_line, chunk.end_line, runs)
            assert chunk.end_line - chunk.start_line + 1 == chunk.text.count("\n") + 1
    texts = [chunk.text for chunk in result.chunks if chunk.kind == "module"]
    assert texts == ["import os", "LATE_CONSTANT = 1", "TAIL = 2"]


def test_no_piece_exceeds_the_limit_after_a_cut_at_a_blank_line():
    first = "".join(f"    alpha_{i:02d} = compute_the_value_for(index_{i:02d})\n" for i in range(10))
    second = "".join(f"    gamma_{i:02d} = compute_the_value_for(index_{i:02d})\n" for i in range(7))
    wide = "    table = [" + ", ".join(str(i) for i in range(160)) + "]\n"
    source = "def staged():\n" + first + "\n" + second + wide + "    return table\n"
    pieces = by_symbol(chunked("python", source).chunks, "staged")
    for piece in pieces:
        assert estimate_tokens(piece.text) <= SPLIT_TOKENS or piece.text.count("\n") == 0, piece.text[:60]


def test_a_cut_forced_by_size_falls_on_the_blank_line_before_it():
    block = "".join(f"    step_{i} = prepare({i})\n" for i in range(12))
    source = "def staged():\n" + (block + "\n") * 6 + "    return 1\n"
    source_lines = source.split("\n")
    pieces = by_symbol(chunked("python", source).chunks, "staged")
    assert len(pieces) > 1
    for piece in pieces[1:]:
        assert source_lines[piece.start_line - 2] == ""  # the line before each later piece is blank
