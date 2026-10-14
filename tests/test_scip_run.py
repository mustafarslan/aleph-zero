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
    import dataclasses

    repo, _ = project
    monkeypatch.setitem(indexers.INDEXERS, "javascript", dataclasses.replace(
        indexers.INDEXERS["javascript"], on_demand=False, why_not="its memory was never measured"))    # as a language would stand before it is
    with pytest.raises(indexers.CannotRun, match="import-only"):
        Engine(repo.root).scip_run("javascript")
    monkeypatch.setitem(indexers.INDEXERS, "python", indexers.Indexer(
        "python", "aleph-no-such-indexer", ("{out}",), on_demand=True, how_to_get="npm install -g @sourcegraph/scip-python"))
    assert cli.main(["--cwd", str(repo.root), "scip", "run", "--lang", "python"]) == 1
    said = capsys.readouterr().err
    assert "aleph-no-such-indexer is not installed" in said and "npm install -g @sourcegraph/scip-python" in said
    failing = stand_in(tmp_path, "import sys\nprint('the build broke')\nsys.exit(3)\n")
    monkeypatch.setitem(indexers.INDEXERS, "python", failing)
    with pytest.raises(indexers.CannotRun, match="the build broke"):
        Engine(repo.root).scip_run("python")


def test_doctor_says_which_indexers_are_here_and_which_languages_are_import_only(project, monkeypatch):
    import dataclasses

    from aleph_zero import doctor

    repo, _ = project
    rows = {row.name: row for row in doctor.check(repo.root)}
    assert "compiler indexes" in rows and "import-only" not in rows["compiler indexes"].detail     # every language is measured now
    assert "3,072 MB" in rows["compiler indexes"].detail                                           # and JavaScript's own limit is said
    monkeypatch.setitem(indexers.INDEXERS, "go", dataclasses.replace(indexers.INDEXERS["go"], on_demand=False, why_not="its memory was never measured"))
    assert "import-only" in {row.name: row for row in doctor.check(repo.root)}["compiler indexes"].detail


def test_a_child_that_outlives_the_indexer_is_stopped_too(project, tmp_path, monkeypatch):
    """The indexer starts something in a session of its own and ends at once: nobody's child any more."""
    repo, made = project
    marker = "aleph-test-orphan-" + tmp_path.name
    leaves = ("import shutil, subprocess, sys, time\n"
              f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)  # {marker}'], start_new_session=True)\n"
              f"time.sleep(0.5)\nshutil.copy({str(made)!r}, sys.argv[-1])\n")
    monkeypatch.setitem(indexers.INDEXERS, "python", stand_in(tmp_path, leaves))
    Engine(repo.root).scip_run("python")
    assert subprocess.run(["pgrep", "-f", marker], capture_output=True).stdout == b""


def test_a_guard_that_cannot_see_stops_the_run(project, tmp_path, monkeypatch):
    repo, made = project
    monkeypatch.setitem(indexers.INDEXERS, "python", stand_in(tmp_path, "import time\nheld = bytearray(300 * 1024 * 1024)\ntime.sleep(20)\n"))
    monkeypatch.setattr(indexers, "PS", ["false"])               # `ps` fails: nothing can be measured
    with pytest.raises(indexers.CannotRun, match="could not watch"):
        Engine(repo.root).scip_run("python", limit_mb=150)


def test_a_limit_that_is_not_a_number_is_refused_and_doctor_still_answers(project, tmp_path, monkeypatch):
    from aleph_zero import doctor

    repo, _ = project
    # An indexer stands in: on a machine without scip-python the run would stop at that first (found by CI).
    monkeypatch.setitem(indexers.INDEXERS, "python", stand_in(tmp_path, "import sys\n"))
    for bad in ("abc", "nan", "0", "-5"):
        monkeypatch.setenv("ALEPH_SCIP_MEMORY_MB", bad)
        with pytest.raises(indexers.CannotRun, match="ALEPH_SCIP_MEMORY_MB"):
            Engine(repo.root).scip_run("python")
        assert any(row.name == "compiler indexes" for row in doctor.check(repo.root))


def test_a_java_run_builds_in_a_copy_and_leaves_the_worktree_as_it_found_it(project, tmp_path, monkeypatch):
    repo, made = project
    repo.write("pom.xml", "<project/>\n")
    (repo.root / "target").mkdir()
    (repo.root / "target" / "kept.class").write_text("the owner's build output")
    cleans = ("import os, shutil, sys\nshutil.rmtree('target', ignore_errors=True)\nopen('made-here.txt', 'w').write('x')\n"
              f"shutil.copy({str(made)!r}, sys.argv[-1])\n")
    indexer = stand_in(tmp_path, cleans)
    monkeypatch.setitem(indexers.INDEXERS, "java", indexers.Indexer("java", indexer.program, indexer.arguments, True, "", needs="pom.xml",
                                                                    in_a_copy=True))
    report = Engine(repo.root).scip_run("java")
    assert report["imported"]["documents"] == 4
    assert (repo.root / "target" / "kept.class").read_text() == "the owner's build output" and not (repo.root / "made-here.txt").exists()


def test_an_import_only_language_runs_when_its_owner_names_the_memory_it_may_take(project, tmp_path, monkeypatch):
    """oxn runs these indexers with no limit at all; Aleph runs them once a limit is named, and watches it."""
    import dataclasses

    repo, made = project
    over = dataclasses.replace(
        stand_in(tmp_path, f"import pathlib, shutil, sys\nmade = pathlib.Path('tsconfig.json')\nmade.exists() or made.write_text('{{}}')\n"
                         f"shutil.copy({str(made)!r}, sys.argv[-1])\n"),
        on_demand=False, why_not="it was measured at 2,295 MB on eslint, over the limit", leaves=("tsconfig.json",))
    monkeypatch.setitem(indexers.INDEXERS, "python", over)
    monkeypatch.delenv("ALEPH_SCIP_MEMORY_MB", raising=False)
    with pytest.raises(indexers.CannotRun, match="ALEPH_SCIP_MEMORY_MB=.* aleph scip run"):
        Engine(repo.root).scip_run("python")
    monkeypatch.setenv("ALEPH_SCIP_MEMORY_MB", "3072")
    report = Engine(repo.root).scip_run("python")
    assert report["imported"]["documents"] == 4 and report["limit_mb"] == 3072
    assert not (repo.root / "tsconfig.json").exists()        # what the indexer left in the worktree is taken away again
    (repo.root / "tsconfig.json").write_text("{\"mine\": true}")
    Engine(repo.root).scip_run("python")
    assert (repo.root / "tsconfig.json").read_text() == "{\"mine\": true}"   # one that was the owner's is not


def test_naming_less_memory_than_the_default_does_not_unlock_an_indexer_measured_above_it(project, tmp_path, monkeypatch):
    import dataclasses

    repo, made = project
    over = dataclasses.replace(stand_in(tmp_path, f"import shutil, sys\nshutil.copy({str(made)!r}, sys.argv[-1])\n"),
                               on_demand=False, why_not="it was measured at 2,295 MB on eslint, over the limit")
    monkeypatch.setitem(indexers.INDEXERS, "python", over)
    monkeypatch.setenv("ALEPH_SCIP_MEMORY_MB", "1024")              # set to lower the ceiling, not to ask for this
    with pytest.raises(indexers.CannotRun, match="above 2048"):
        Engine(repo.root).scip_run("python")
    monkeypatch.setenv("ALEPH_SCIP_MEMORY_MB", "3072")
    engine = Engine(repo.root)
    engine.search("own")                                            # indexed: python is the worktree's main language
    with pytest.raises(indexers.CannotRun, match="--lang python"):  # and never because it happens to be the main language
        engine.scip_run()
    assert Engine(repo.root).scip_run("python")["imported"]["documents"] == 4


def test_a_file_that_appears_during_the_run_and_is_not_what_the_indexer_infers_is_left(project, tmp_path, monkeypatch):
    """The indexer infers `{}` or `{"compilerOptions":{"allowJs":true}}`. Anything else of that name was written
    by the owner while it ran, and is theirs."""
    import dataclasses

    repo, made = project
    script = (f"import pathlib, shutil, sys\npathlib.Path('tsconfig.json').write_text(CONTENT)\n"
              f"shutil.copy({str(made)!r}, sys.argv[-1])\n")
    monkeypatch.setenv("ALEPH_SCIP_MEMORY_MB", "3072")
    for content, stays in (('{"compilerOptions":{"allowJs":true}}', False), ('{}\n', False), ('{"compilerOptions": {"strict": true}}', True)):
        (repo.root / "tsconfig.json").unlink(missing_ok=True)
        stand = dataclasses.replace(stand_in(tmp_path, script.replace("CONTENT", repr(content))), on_demand=False, leaves=("tsconfig.json",))
        monkeypatch.setitem(indexers.INDEXERS, "python", stand)
        try:
            Engine(repo.root).scip_run("python")
        except indexers.CannotRun:
            pass        # a file of the owner's that appeared makes the run's result unusable; it must still be there
        assert (repo.root / "tsconfig.json").exists() is stays, content


def test_the_languages_run_on_request_are_those_measured_under_the_limit():
    """ADR-004's table, as the code has it: TypeScript since its indexer was measured (694 MB on rxjs); JavaScript was over."""
    on_request = {language for language, indexer in indexers.INDEXERS.items() if indexer.on_demand}
    assert on_request == {"python", "go", "rust", "java", "typescript", "javascript"}
    typescript = indexers.INDEXERS["typescript"]
    assert typescript.needs == "tsconfig.json" and "installed" in typescript.note and not typescript.why_not


def test_javascript_runs_on_request_under_a_limit_of_its_own(monkeypatch):
    """Its indexer was measured at 2,295 MB on eslint, over the 2,048 MB the others are held to. It is given 3,072 and
    watched like them; the others keep their limit. A limit the owner names is the limit for every language."""
    monkeypatch.delenv("ALEPH_SCIP_MEMORY_MB", raising=False)
    javascript = indexers.INDEXERS["javascript"]
    assert javascript.on_demand and javascript.limit_mb == 3072.0 and "2,295 MB" in javascript.note and not javascript.why_not
    assert indexers.limit_for("javascript") == 3072.0
    assert {indexers.limit_for(language) for language in ("python", "go", "rust", "java", "typescript")} == {indexers.LIMIT_MB} == {2048.0}
    monkeypatch.setenv("ALEPH_SCIP_MEMORY_MB", "1024")
    assert indexers.limit_for("javascript") == 1024.0 and indexers.limit_for("python") == 1024.0      # a smaller machine's ceiling holds for all
