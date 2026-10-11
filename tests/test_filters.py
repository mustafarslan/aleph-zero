from aleph_zero import filters


def test_markdown_is_a_doc_and_code_is_text():
    assert filters.classify("docs/a.md", b"# Title\n") == ("doc", "# Title\n")
    assert filters.classify("src/a.py", b"x = 1\n") == ("text", "x = 1\n")


def test_unindexable_files_are_skipped_with_a_reason():
    assert filters.classify("package-lock.json", b"{}") == ("skip:lockfile", None)
    assert filters.classify("a.bin", b"\x00\x01\x02") == ("skip:binary", None)
    assert filters.classify("a.txt", b"x" * (filters.MAX_BYTES + 1)) == ("skip:too-large", None)
    assert filters.classify("m.psd", b"version https://git-lfs.github.com/spec/v1\noid sha256:ab\n") == ("skip:lfs-pointer", None)
    assert filters.classify("a.py", b"\xff\xfe broken") == ("skip:not-utf8", None)
    assert filters.classify("a.py", b"  \n\n") == ("skip:empty", None)
    assert filters.classify("app.min.js", b"a=1;" * 2000) == ("skip:minified", None)
