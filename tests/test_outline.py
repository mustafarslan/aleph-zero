import json
import shutil
from pathlib import Path

import pytest

from aleph_zero import cli, outline
from aleph_zero.chunking import estimate_tokens
from aleph_zero.view import build_view

SAMPLES = Path(__file__).parent / "samples"


def _outline(repo, path):
    return outline.of(build_view(repo.root), path)


def test_a_source_file_is_outlined_by_its_definitions(repo):
    shutil.copy(SAMPLES / "sample.py", repo.root / "src/sample.py")
    result = _outline(repo, "src/sample.py")
    assert (result.path, result.family, result.lines) == ("src/sample.py", "python", 30)
    rows = [(item.depth, item.kind, item.qualified, item.start_line, item.end_line) for item in result.items]
    assert rows == [
        (0, "constant", "MAX_RETRY", 6, 6), (0, "variable", "_cache", 7, 7), (0, "function", "refresh_token", 10, 12),
        (0, "class", "TokenStore", 15, 30), (1, "field", "TokenStore.ttl", 18, 18),
        (1, "method", "TokenStore.__init__", 20, 21), (1, "method", "TokenStore.size", 23, 25),
        (1, "method", "TokenStore.make", 27, 30),
        (2, "function", "TokenStore.make.handler", 29, 29),          # defined inside `make`: shown under it (2026-10-15)
    ]
    assert [item.nested for item in result.items][-2:] == [False, True]
    text = outline.text(result)
    assert text.split("\n")[0] == "src/sample.py  python  30 lines"
    assert "  10-12  function  def refresh_token(session: Session, *, force: bool = False) -> str" in text
    assert "    23-25  method  @property def size(self) -> int" in text  # nested one level deeper


def test_documentation_is_outlined_by_its_headings(repo):
    result = _outline(repo, "docs/guide.md")
    assert [(item.depth, item.kind, item.name, item.start_line) for item in result.items] == [
        (0, "section", "Guide", 1), (1, "section", "Authentication", 5), (1, "section", "Billing", 9)]
    repo.write("docs/api.rst", "API\n===\n\nIntro.\n\nCalls\n-----\n\nText.\n")
    assert [(item.depth, item.name) for item in _outline(repo, "docs/api.rst").items] == [(0, "API"), (1, "Calls")]


def test_an_uncommitted_edit_is_outlined_from_disk(repo):
    repo.write("src/billing/invoice.py", "def render_receipt(order):\n    return 1\n")
    assert [item.qualified for item in _outline(repo, "src/billing/invoice.py").items] == ["render_receipt"]


def test_a_file_without_structure_and_a_path_outside_the_view(repo):
    repo.write("data/values.csv", "a,b\n1,2\n")
    plain = _outline(repo, "data/values.csv")
    assert plain.items == [] and outline.text(plain) == "data/values.csv  text  2 lines\n  (no structure found)"
    with pytest.raises(outline.OutlineError, match="not a file in this worktree"):
        _outline(repo, "src/missing.py")
    (repo.root / "logo.bin").write_bytes(b"\x00\x01")
    with pytest.raises(outline.OutlineError, match="binary"):
        _outline(repo, "logo.bin")


def test_a_very_large_outline_stays_inside_its_budget(repo):
    repo.write("gen/big.py", "".join(
        f"class Model{i}:\n    def save_{i}(self):\n        return {i}\n\n    def load_{i}(self):\n        return {i}\n\n\n"
        for i in range(1700)))
    result = _outline(repo, "gen/big.py")
    assert len(result.items) == 5100
    text = outline.text(result, budget_tokens=1500)
    assert estimate_tokens(text) <= 1500
    assert "class Model0" in text and "more definitions not shown" in text
    assert "save_0" not in text  # nested definitions give way to top-level ones first


def test_outline_command(repo, capsys):
    assert cli.main(["--cwd", str(repo.root), "outline", "src/auth/token_store.py"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("src/auth/token_store.py  python  11 lines") and "function  def refresh_token(session)" in out
    assert cli.main(["--cwd", str(repo.root), "outline", "src/auth/token_store.py", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == "src/auth/token_store.py" and payload["items"][0]["kind"] == "constant"
    assert cli.main(["--cwd", str(repo.root), "outline", "nope.py"]) == 2
    assert "not a file in this worktree" in capsys.readouterr().err


def test_two_sections_with_the_same_heading_are_two_items_and_hidden_counts_are_true(repo):
    repo.write("docs/twice.md", "# A\n\n## X\n\nfirst\n\n## X\n\nsecond\n")
    assert [(item.name, item.start_line) for item in _outline(repo, "docs/twice.md").items] == [("A", 1), ("X", 3), ("X", 7)]
    repo.write("gen/flat.py", "".join(f"def function_number_{i}(argument_one, argument_two):\n    return {i}\n\n\n" for i in range(3000)))
    text = outline.text(_outline(repo, "gen/flat.py"), budget_tokens=600)
    shown = text.count("  function  ")
    assert f"(+{3000 - shown} more definitions not shown)" in text


def test_what_is_defined_inside_functions_is_the_first_to_go_when_a_file_does_not_fit(repo):
    body = "".join(f"def top_{n}(x):\n    def inner_{n}():\n        return x\n    return inner_{n}()\n\n\n" for n in range(40))
    repo.write("src/many.py", body)
    repo.commit("many")
    result = _outline(repo, "src/many.py")
    assert sum(item.nested for item in result.items) == 40
    shown = outline.text(result, budget_tokens=500)
    assert "top_0" in shown and "inner_0" not in shown and "more definitions not shown" in shown
