from aleph_zero import render
from aleph_zero.chunking import estimate_tokens
from aleph_zero.search import Hit


def hit(path="src/a.py", start=10, end=14, kind="function", symbol="load", signature="def load(path)",
        breadcrumb="Store.load", text=None, score=0.05, duplicates=0, scope=None):
    text = text if text is not None else "def load(path):\n    data = read(path)\n    check(data)\n    cache[path] = data\n    return data"
    scope = scope or (start, end)
    return Hit(path, start, end, kind, symbol, signature, breadcrumb, text, score, ("lexical",), scope[0], scope[1], duplicates)


def answer(*hits, **extra):
    return render.Answer(query="load data", hits=list(hits), **extra)


def test_a_hit_is_a_location_line_a_signature_and_its_text():
    out = render.text(answer(hit()))
    lines = out.split("\n")
    assert lines[0] == "src/a.py:10-14  function  Store.load"
    assert lines[1] == "  def load(path)"
    assert lines[2:7] == ["    def load(path):", "        data = read(path)", "        check(data)",
                          "        cache[path] = data", "        return data"]


def test_locations_only_and_full_detail():
    long_text = "\n".join(f"line {i}" for i in range(40))
    brief = render.text(answer(hit(text=long_text, end=49)), detail="locations")
    assert brief == "src/a.py:10-49  function  Store.load\n  def load(path)"
    snippets = render.text(answer(hit(text=long_text, end=49)))
    assert "line 0" in snippets and "line 39" not in snippets and "(+32 lines)" in snippets
    full = render.text(answer(hit(text=long_text, end=49)), detail="full")
    assert "line 39" in full and "(+" not in full


def test_the_answer_never_exceeds_its_budget_and_says_what_it_left_out():
    hits = [hit(path=f"src/module_{i}.py", symbol=f"load_{i}", breadcrumb=f"load_{i}",
                text="\n".join(f"    value_{j} = compute({j})" for j in range(30))) for i in range(40)]
    for budget in (60, 200, 500, 1500):
        for detail in ("locations", "snippets", "full"):
            out = render.text(answer(*hits), budget_tokens=budget, detail=detail)
            assert estimate_tokens(out) <= budget, (budget, detail)
    out = render.text(answer(*hits), budget_tokens=200)
    assert "src/module_0.py" in out and "more results not shown" in out
    shown = out.count("  function  ")
    assert f"(+{40 - shown} more results not shown" in out


def test_higher_ranked_hits_get_their_text_first():
    hits = [hit(path=f"src/m{i}.py", breadcrumb=f"f{i}", text="\n".join(f"    body_{i}_{j} = {j}" for j in range(8)))
            for i in range(10)]
    out = render.text(answer(*hits), budget_tokens=260)
    assert "body_0_0" in out and "body_9_0" not in out
    assert out.index("src/m0.py") < out.index("src/m9.py")  # every location still listed, in rank order


def test_touching_pieces_of_one_section_are_one_entry():
    first = hit(path="docs/guide.md", start=1, end=40, kind="doc", symbol="Install", signature="Install",
                breadcrumb="Guide > Install", text="\n".join(f"a{i}" for i in range(40)))
    second = hit(path="docs/guide.md", start=41, end=60, kind="doc", symbol="Install", signature="Install",
                 breadcrumb="Guide > Install", text="\n".join(f"b{i}" for i in range(20)), score=0.04)
    other = hit(path="docs/guide.md", start=90, end=95, kind="doc", symbol="Usage", signature="Usage",
                breadcrumb="Guide > Usage", text="usage text", score=0.03)
    out = render.text(answer(first, second, other), detail="full", budget_tokens=4000)
    assert out.count("Guide > Install") == 1 and "docs/guide.md:1-60  doc  Guide > Install" in out
    assert "a39" in out and "b0" in out and out.index("a39") < out.index("b0")
    assert "docs/guide.md:90-95  doc  Guide > Usage" in out


def test_identical_text_in_several_files_is_printed_once():
    same = "def helper():\n    return 1"
    hits = [hit(path=p, start=3, end=4, symbol="helper", breadcrumb="helper", signature="def helper()", text=same,
                duplicates=5 if p == "a/util.py" else 0) for p in ("a/util.py", "b/util.py", "c/util.py")]
    out = render.text(answer(*hits))
    assert out.count("return 1") == 1
    assert "a/util.py:3-4  function  helper" in out
    assert "  also in: b/util.py:3, c/util.py:3 (+5 more files)" in out


def test_a_piece_of_a_long_definition_says_where_the_whole_is():
    out = render.text(answer(hit(start=120, end=140, scope=(100, 400))))
    assert out.split("\n")[0] == "src/a.py:120-140  function  Store.load  (part of 100-400)"


def test_text_already_shown_in_this_session_is_replaced_by_a_pointer():
    shown = hit()
    out = render.text(answer(shown, hit(path="src/b.py", breadcrumb="other", text="x = 1")),
                      already_shown=lambda h: h.path == "src/a.py")
    assert "src/a.py:10-14  function  Store.load" in out and "    (shown earlier in this session)" in out
    assert "data = read(path)" not in out and "x = 1" in out
    assert "data = read(path)" in render.text(answer(shown), detail="full", already_shown=lambda h: True)


def test_pending_files_timeouts_and_empty_results_are_stated():
    out = render.text(answer(hit(), pending=12, timed_out=True, elapsed_ms=250))
    assert "note: 12 files are not indexed yet; results may be incomplete" in out
    assert "note: stopped at the time limit (250 ms); results may be incomplete" in out
    assert render.text(answer()) == 'no results for "load data"'
    assert render.text(answer(pending=3)).startswith('no results for "load data"\nnote: 3 files are not indexed yet')


def test_json_form_carries_the_same_facts():
    same = "def helper():\n    return 1"
    payload = render.as_json(answer(hit(path="a/util.py", text=same, duplicates=2), hit(path="b/util.py", text=same),
                                    pending=1))
    assert payload["query"] == "load data" and payload["pending"] == 1 and payload["timed_out"] is False
    assert [h["path"] for h in payload["hits"]] == ["a/util.py", "b/util.py"]
    assert payload["hits"][0]["duplicates"] == 2 and payload["hits"][0]["text"] == same
    assert set(payload["hits"][0]) >= {"path", "start_line", "end_line", "scope_start_line", "scope_end_line",
                                       "kind", "symbol", "signature", "breadcrumb", "score", "sources"}
