from aleph_zero.chunking import Chunk, chunk_file, chunk_hash, estimate_tokens, markdown_chunks, normalize, rst_chunks, window_chunks


def _chunk(text, breadcrumb="", start=1):
    return Chunk("block", "", "", breadcrumb, "", text, start, start + text.count("\n"))


def test_normalize_ignores_line_endings_trailing_space_and_common_indentation():
    assert normalize("    a = 1  \r\n    if a:\r\n        b = 2\r\n") == "a = 1\nif a:\n    b = 2"


def test_hash_ignores_position_and_indentation_but_not_content_or_scope():
    base = chunk_hash("text", _chunk("a = 1\nb = 2"))
    assert chunk_hash("text", _chunk("    a = 1\n    b = 2", start=90)) == base
    assert chunk_hash("text", _chunk("a = 1\nb = 3")) != base
    assert chunk_hash("text", _chunk("a = 1\nb = 2", breadcrumb="Other")) != base
    assert chunk_hash("doc", _chunk("a = 1\nb = 2")) != base


def test_windows_cover_every_line_and_report_its_number():
    text = "".join(f"line {i}\n" for i in range(1, 96))
    chunks = window_chunks(text)
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 40), (31, 70), (61, 95)]
    covered = {n for c in chunks for n in range(c.start_line, c.end_line + 1)}
    assert covered == set(range(1, 96))
    assert chunks[1].text.split("\n")[0] == "line 31"


def test_short_file_is_one_window_and_blank_file_is_none():
    assert [(c.start_line, c.end_line) for c in window_chunks("a\nb\n")] == [(1, 2)]
    assert window_chunks("\n\n\n") == []


def test_markdown_sections_carry_the_heading_trail():
    text = "intro\n\n# Guide\n\ntext\n\n## Install\n\npip\n\n### Linux\n\napt\n\n## Usage\n\nrun\n"
    chunks = markdown_chunks(text)
    assert [c.breadcrumb for c in chunks] == ["", "Guide", "Guide > Install", "Guide > Install > Linux", "Guide > Usage"]
    install = chunks[2]
    assert (install.symbol, install.signature, install.kind) == ("Install", "Install", "doc")
    assert (install.start_line, install.end_line) == (7, 9)
    assert install.text == "## Install\n\npip"


def test_hash_inside_a_code_fence_is_not_a_heading():
    text = "# Real\n\n```sh\n# not a heading\necho hi\n```\n\nafter\n"
    chunks = markdown_chunks(text)
    assert [c.breadcrumb for c in chunks] == ["Real"]
    assert "# not a heading" in chunks[0].text


def test_long_markdown_section_is_split_and_keeps_its_breadcrumb():
    text = "# Big\n" + "".join(f"row {i}\n" for i in range(100))
    chunks = markdown_chunks(text)
    assert len(chunks) == 3 and {c.breadcrumb for c in chunks} == {"Big"}
    assert chunks[0].start_line == 1 and chunks[-1].end_line == 101


def test_chunk_file_picks_the_chunker_by_family():
    assert chunk_file("markdown", "doc", "# T\n\nbody\n")[0].breadcrumb == "T"
    assert chunk_file("prose", "doc", "plain\n")[0].kind == "doc"
    assert chunk_file("text", "text", "x = 1\n")[0].kind == "block"


def test_token_estimate_uses_the_measured_ratio():
    assert estimate_tokens("x" * 1000) == 292


def test_windows_line_endings_do_not_change_line_numbers_or_the_hash():
    unix = "".join(f"line {i}\n" for i in range(1, 11))
    windows = unix.replace("\n", "\r\n")
    assert [(c.start_line, c.end_line) for c in window_chunks(windows)] == [(1, 10)]
    assert chunk_hash("text", window_chunks(windows)[0]) == chunk_hash("text", window_chunks(unix)[0])


def test_setext_headings_open_sections():
    text = "Guide\n=====\n\nIntro text.\n\nInstall\n-------\n\nRun the installer.\n\n### Details\n\nMore.\n"
    chunks = markdown_chunks(text)
    assert [chunk.breadcrumb for chunk in chunks] == ["Guide", "Guide > Install", "Guide > Install > Details"]
    assert chunks[1].symbol == "Install" and chunks[1].start_line == 6 and chunks[1].signature == "Install"


def test_a_thematic_break_a_table_and_front_matter_are_not_headings():
    text = ("---\ntitle: x\ntags: y\n---\n\n# Real\n\nPara one.\n\n---\n\nPara two.\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n- item\n-\n")
    assert [chunk.breadcrumb for chunk in markdown_chunks(text)] == ["", "Real"]


def test_restructuredtext_sections_nest_by_the_order_adornments_appear():
    text = ("=========\nQuerySets\n=========\n\nIntro.\n\nMethods\n=======\n\nAbout methods.\n\n"
            "filter\n------\n\nFilters rows.\n\nexclude\n-------\n\nExcludes rows.\n\nSettings\n========\n\nMore.\n")
    chunks = rst_chunks(text)
    assert [chunk.breadcrumb for chunk in chunks] == [
        "QuerySets", "QuerySets > Methods", "QuerySets > Methods > filter",
        "QuerySets > Methods > exclude", "QuerySets > Settings",
    ]
    assert chunks[0].start_line == 1 and chunks[0].signature == "QuerySets"
    assert chunks[2].symbol == "filter" and chunks[2].kind == "doc" and "Filters rows." in chunks[2].text


def test_rst_tables_transitions_and_short_underlines_are_not_headings():
    text = ("Title\n=====\n\n=====  =====\nA      B\n=====  =====\n1      2\n=====  =====\n\nText\n\n----\n\n"
            "More text\n\nNot a title\n--\n\n::\n\n    code\n    ====\n")
    assert [chunk.breadcrumb for chunk in rst_chunks(text)] == ["Title"]


def test_prose_is_chunked_by_restructuredtext_headings_and_by_window_without_them():
    assert chunk_file("prose", "doc", "Usage\n=====\n\nRun it.\n")[0].breadcrumb == "Usage"
    plain = chunk_file("prose", "doc", "just a line\n" * 100)
    assert len(plain) > 1 and all(chunk.kind == "doc" and chunk.breadcrumb == "" for chunk in plain)
    assert sum(chunk.text.count("just a line") for chunk in plain) == 100
