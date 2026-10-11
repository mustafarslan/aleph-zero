from aleph_zero import filters


def classify(path, data):
    return filters.classify(filters.family(path), data)


def test_family_is_the_treatment_a_path_selects():
    assert filters.family("docs/guide.md") == "markdown"
    assert filters.family("README.MDX") == "markdown"
    assert filters.family("notes.txt") == "prose"
    assert filters.family("pkg/uv.lock") == "lockfile"
    assert filters.family("Makefile") == "text"


def test_markdown_is_a_doc_and_code_is_text():
    assert classify("docs/a.md", b"# Title\n") == ("doc", "# Title\n")
    assert classify("Makefile", b"x = 1\n") == ("text", "x = 1\n")


def test_unindexable_files_are_skipped_with_a_reason():
    assert classify("package-lock.json", b"{}") == ("skip:lockfile", None)
    assert classify("a.bin", b"\x00\x01\x02") == ("skip:binary", None)
    assert classify("a.txt", b"x" * (filters.MAX_BYTES + 1)) == ("skip:too-large", None)
    assert classify("m.psd", b"version https://git-lfs.github.com/spec/v1\noid sha256:ab\n") == ("skip:lfs-pointer", None)
    assert classify("a.py", b"\xff\xfe broken") == ("skip:not-utf8", None)
    assert classify("a.py", b"  \n\n") == ("skip:empty", None)
    assert classify("app.min.js", b"a=1;" * 2000) == ("skip:minified", None)


def test_a_byte_order_mark_is_not_part_of_the_text():
    assert classify("README.md", "﻿# Top\n".encode()) == ("doc", "# Top\n")
