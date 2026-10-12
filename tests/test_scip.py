"""Reading a SCIP index (the oracle for the code graph)."""
from pathlib import Path

import pytest

from aleph_zero import scip


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(number: int, payload: bytes | int) -> bytes:
    if isinstance(payload, int):
        return varint(number << 3) + varint(payload)
    return varint(number << 3 | 2) + varint(len(payload)) + payload


def occurrence(symbol: str, span: list[int], roles: int = 0) -> bytes:
    return field(1, b"".join(varint(n) for n in span)) + field(2, symbol.encode()) + (field(3, roles) if roles else b"")


def document(path: str, *occurrences: bytes, symbols: list[bytes] = ()) -> bytes:
    return field(1, path.encode()) + b"".join(field(2, o) for o in occurrences) + b"".join(field(3, s) for s in symbols)


def index(*documents: bytes) -> bytes:
    return field(1, b"metadata the reader does not need") + b"".join(field(2, d) for d in documents)


def test_an_index_is_read_into_documents_occurrences_and_roles():
    data = index(
        document("pkg/a.py",
                 occurrence("scip-python python demo 0.1 pkg/a/helper().", [3, 4, 10], scip.ROLE_DEFINITION),
                 occurrence("scip-python python demo 0.1 pkg/a/helper().", [9, 11, 9, 17]),
                 symbols=[field(1, b"scip-python python demo 0.1 pkg/a/helper().") + field(6, b"helper")]),
        document("pkg/b.py", occurrence("scip-python python demo 0.1 pkg/a/helper().", [1, 0, 6], scip.ROLE_IMPORT)),
    )
    found = scip.parse_index(data)
    assert [doc.relative_path for doc in found.documents] == ["pkg/a.py", "pkg/b.py"]
    first, second = found.documents[0].occurrences
    assert (first.start_line, first.start_char, first.end_line, first.end_char, first.is_definition) == (3, 4, 3, 10, True)
    assert (second.start_line, second.end_line, second.is_definition) == (9, 9, False)   # a four-number span is used as given
    assert found.documents[1].occurrences[0].is_import
    assert found.documents[0].symbols[0].display_name == "helper"
    assert found.occurrence_count == 3


def test_a_file_described_twice_is_kept_once_and_counted():
    twice = index(document("a.py", occurrence("s1", [0, 0, 1])), document("a.py", occurrence("s1", [0, 0, 1])))
    found = scip.parse_index(twice)
    assert len(found.documents) == 1 and found.duplicate_documents == 1


def test_fields_the_reader_does_not_know_are_skipped():
    extra = field(99, b"something from a later version of the schema") + field(98, 12345)
    found = scip.parse_index(index(document("a.py", occurrence("s1", [0, 0, 1]) + extra)) + extra)
    assert found.documents[0].occurrences[0].symbol == "s1"


@pytest.mark.parametrize("cut", [1, 7, 20, -3])
def test_a_truncated_index_is_an_error_not_a_shorter_index(cut):
    data = index(document("pkg/a.py", occurrence("scip-python python demo 0.1 pkg/a/helper().", [3, 4, 10], 1)))
    with pytest.raises(scip.WireFormatError):
        scip.parse_index(data[:cut])


def test_a_fixed_width_field_that_runs_past_the_end_is_an_error():
    with pytest.raises(scip.WireFormatError):
        list(scip.iter_fields(varint(5 << 3 | 1) + b"\x00\x00\x00"))      # eight bytes promised, three there
    with pytest.raises(scip.WireFormatError):
        list(scip.iter_fields(varint(5 << 3 | 5) + b"\x00"))


REAL = Path("~/Projects/oxn/benchmarks/.scip-cache/python-httpx.scip")


@pytest.mark.skipif(not REAL.exists(), reason="no real SCIP index on this machine")
def test_a_real_index_from_scip_python_is_read():
    found = scip.load_index(REAL)
    assert len(found.documents) > 20 and found.occurrence_count > 10_000
    assert any(occurrence.is_definition for document in found.documents for occurrence in document.occurrences)
