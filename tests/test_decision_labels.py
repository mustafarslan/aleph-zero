"""The rule by which development pairs for decision retrieval are drawn from a repository's history."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import decision_labels as labels  # noqa: E402


def _commit(root: Path, message: str, files: dict[str, str], author: str = "Dev <dev@example.com>") -> str:
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q",
                    f"--author={author}", "-m", message], check=True)
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def history(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _commit(root, "the records as they stand", {
        "docs/adr/0001-use-queues.md": "# 1. Use queues for background work\n\n## Status\n\nAccepted\n",
        "docs/adr/adr002-static-pages.md": "---\nid: adrs-adr002\ntitle: 'ADR002: Render pages statically'\n---\n\n## Context\n\nSpeed.\n",
        "docs/adr/core/ADR-003-handlers-behind-facade.md": "# ADR 003: Handlers behind a facade\n\nText.\n",
        "docs/adr/adr000-template.md": "---\nid: adrs-adr000\ntitle: 'ADR000: [TITLE]'\n---\n\n## Context\n",
        "docs/adr/README.md": "# Records\n",
        "src/a.py": "x = 1\n"})
    return root


def test_a_pair_is_a_commit_that_cites_a_record_while_changing_something_else(history):
    worked = _commit(history, "feat: route the nightly jobs through the worker queue per ADR-0001", {"src/a.py": "x = 2\n"})
    nested = _commit(history, "refactor: move every request handler behind one entry point as ADR003 says", {"src/b.py": "y = 1\n"})
    fronted = _commit(history, "feat: prebuild the landing pages at deploy time, following ADR 2", {"src/c.py": "z = 1\n"})
    _commit(history, "Update adr-0001-use-queues.md wording about retries and ordering", {"docs/adr/0001-use-queues.md": "# 1. Use queues for background work\n\nMore.\n"})
    _commit(history, "build(deps): bump the queue library to its next major, see ADR-0001 notes", {"src/a.py": "x = 3\n"})
    _commit(history, "chore: refresh pinned versions across services (ADR-0001 compliant)", {"src/a.py": "x = 4\n"}, author="dependabot[bot] <bot@example.com>")
    _commit(history, "feat: start the new service from the shared template as ADR000 lays out", {"src/d.py": "w = 1\n"})
    head = subprocess.run(["git", "-C", str(history), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    pairs, records = labels.pairs_in(history, head, "docs/adr+docs/adr/core")
    assert records == {"docs/adr/0001-use-queues.md": 1, "docs/adr/adr002-static-pages.md": 2, "docs/adr/core/ADR-003-handlers-behind-facade.md": 3}
    assert {pair["sha"]: pair["gold"] for pair in pairs} == {worked: [1], nested: [3], fronted: [2]}
    assert all(pair["restates_title"] is False for pair in pairs)


def test_what_was_left_out_and_why_is_counted(history):
    _commit(history, "Update adr-0001-use-queues.md wording about retries and ordering", {"docs/adr/0001-use-queues.md": "# 1. Use queues for background work\n\nMore.\n"})
    _commit(history, "build(deps): bump the queue library to its next major, see ADR-0001 notes", {"src/a.py": "x = 3\n"})
    head = subprocess.run(["git", "-C", str(history), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    left = {}
    assert labels.pairs_in(history, head, "docs/adr", left)[0] == []
    assert left == {"only the records changed": 1, "a bot or a dependency bump": 1}


def test_a_record_written_where_the_records_used_to_be_is_not_work_it_governs(history):
    _commit(history, "Flesh out ADR 1 with the retries and the ordering spelled out in full", {
        "docs/architecture/adr-001-use-queues.md": "# ADR 001: Use queues\n", "docs/architecture/img/queues.png": "png"})
    head = subprocess.run(["git", "-C", str(history), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    left = {}
    assert labels.pairs_in(history, head, "docs/adr", left)[0] == [] and left == {"only the records changed": 1}


def test_the_reference_ranker_reads_a_record_with_a_header_as_oxn_does():
    import decisions as harness

    texts = {"adr/adr006-avoid-fc.md": "---\nid: adrs-adr006\ntitle: 'ADR006: Avoid the FC helper'\ntags: [react, typing]\n---\n\n## Context\n\nComponents.\n",
             "adr/0007-use-queues.md": "# 7. Use queues\n\nBackground work goes through the queue.\n",
             "adr/0008-broken.md": "---\ntitle: [unclosed\n---\n\nNothing oxn can read: no header it understands and no heading.\n"}
    numbers = {"adr/adr006-avoid-fc.md": 6, "adr/0007-use-queues.md": 7, "adr/0008-broken.md": 8}
    assert harness.reference_order(texts, numbers, "avoid FC helper components") == [6]       # the title is in the header only
    assert harness.reference_order(texts, numbers, "typing react") == [6]                        # and so are the tags
    assert harness.reference_order(texts, numbers, "background queue") == [7]
    assert harness.reference_order(texts, numbers, "nothing understands heading") == []                     # as oxn: skipped, not fatal
