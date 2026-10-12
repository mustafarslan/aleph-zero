"""Running an indexer on request: only where it was measured, watched for memory, and believed only
if the worktree stood still meanwhile."""
import subprocess
import sys

import pytest

from aleph_zero import cli, indexers
from aleph_zero.engine import Engine
from test_scip_import import ALONE_PY, made_index, project  # noqa: F401 - the fixture


def stand_in(tmp_path, body: str) -> indexers.Indexer:
    """An "indexer" that is a Python script: it gets the output path as its last argument."""
    script = tmp_path / "indexer.py"
    script.write_text(body)
    return indexers.Indexer("python", sys.executable, (str(script), "{out}"), on_demand=True, how_to_get="(a test stand-in)")


def test_a_run_imports_what_the_indexer_wrote_stamped_with_the_worktree(project, tmp_path, monkeypatch):
    repo, made = project
    repo.write("src/alone.py", ALONE_PY + "\n# not committed: the indexer sees it\n")
    monkeypatch.setitem(indexers.INDEXERS, "python", stand_in(tmp_path, f"import shutil, sys\nshutil.copy({str(made)!r}, sys.argv[-1])\n"))
    report = Engine(repo.root).scip_run("python")
    assert report["imported"]["documents"] == 4 and report["imported"]["stamped"] == "worktree" and report["peak_mb"] > 0
    found = Engine(repo.root).references("caller", direction="callees").callees
    assert [(r.target, r.level) for r in found] == [("own", "L2")]


def test_an_indexer_over_the_limit_is_stopped_with_everything_it_started(project, tmp_path, monkeypatch):
    repo, _ = project
    marker = "aleph-test-child-" + tmp_path.name
    greedy = ("import subprocess, sys, time\n"
              f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)  # {marker}'], start_new_session=True)\n"
              "held = bytearray(400 * 1024 * 1024)\ntime.sleep(30)\n")
    monkeypatch.setitem(indexers.INDEXERS, "python", stand_in(tmp_path, greedy))
    with pytest.raises(indexers.CannotRun, match="over 150 MB"):
        Engine(repo.root).scip_run("python", limit_mb=150)
    assert subprocess.run(["pgrep", "-f", marker], capture_output=True).stdout == b""     # the child in a session of its own went too
    assert Engine(repo.root).scip_imports() == []


def test_a_worktree_that_changed_while_the_indexer_ran_is_not_stamped(project, tmp_path, monkeypatch):
    repo, made = project
    edits = (f"import shutil, sys\nopen({str(repo.root / 'src/alone.py')!r}, 'a').write('# changed under the indexer\\n')\n"
             f"shutil.copy({str(made)!r}, sys.argv[-1])\n")
    monkeypatch.setitem(indexers.INDEXERS, "python", stand_in(tmp_path, edits))
    with pytest.raises(indexers.CannotRun, match="changed while"):
        Engine(repo.root).scip_run("python")
    assert Engine(repo.root).scip_imports() == []


def test_a_language_that_was_not_measured_or_an_indexer_that_is_absent_is_said_not_run(project, tmp_path, monkeypatch, capsys):
    repo, _ = project
    with pytest.raises(indexers.CannotRun, match="import-only"):
        Engine(repo.root).scip_run("rust")                                   # known, not measured under the limit
    monkeypatch.setitem(indexers.INDEXERS, "python", indexers.Indexer(
        "python", "aleph-no-such-indexer", ("{out}",), on_demand=True, how_to_get="npm install -g @sourcegraph/scip-python"))
    assert cli.main(["--cwd", str(repo.root), "scip", "run", "--lang", "python"]) == 1
    said = capsys.readouterr().err
    assert "aleph-no-such-indexer is not installed" in said and "npm install -g @sourcegraph/scip-python" in said
    failing = stand_in(tmp_path, "import sys\nprint('the build broke')\nsys.exit(3)\n")
    monkeypatch.setitem(indexers.INDEXERS, "python", failing)
    with pytest.raises(indexers.CannotRun, match="the build broke"):
        Engine(repo.root).scip_run("python")


def test_doctor_says_which_indexers_are_here_and_which_languages_are_import_only(project):
    from aleph_zero import doctor

    repo, _ = project
    rows = {row.name: row for row in doctor.check(repo.root)}
    assert "compiler indexes" in rows and "import-only" in rows["compiler indexes"].detail
