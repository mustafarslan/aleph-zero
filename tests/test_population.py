"""Which language the product would call a benchmark question's repository, written into the result that scored it."""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench" / "eval"))
import population  # noqa: E402


def _commit(clone: Path, files: dict[str, str]) -> str:
    git = lambda *args: subprocess.run(["git", "-C", str(clone), *args], capture_output=True, text=True, check=True).stdout.strip()  # noqa: E731
    if not (clone / ".git").exists():
        clone.mkdir(parents=True)
        git("init", "-q")
    for name, text in files.items():
        (clone / name).write_text(text)
    git("add", "-A")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "more")
    return git("rev-parse", "HEAD")


def test_a_question_gets_the_main_language_of_its_repository_at_its_own_commit(tmp_path):
    # The label of a question set says what the fix touched; the product goes by what most of the repository is written in,
    # at that commit: a repository that moved from JavaScript to TypeScript is one and then the other.
    clone = tmp_path / "o__r"
    early = _commit(clone, {"a.js": "export function alpha() { return 1 }\n" * 40, "types.d.ts": "export declare function alpha(): number\n",
                            "README.md": "# words\n" * 400})
    late = _commit(clone, {"b.ts": "export function beta(): number { return 2 }\n" * 200})
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"summary": {"instances": 2}, "instances": [
        {"instance": "o__r-1", "repo": "o/r", "scores": {}}, {"instance": "o__r-2", "repo": "o/r", "scores": {}}]}))
    questions = [{"instance_id": "o__r-1", "repo": "o/r", "base_commit": early}, {"instance_id": "o__r-2", "repo": "o/r", "base_commit": late},
                 {"instance_id": "o__r-3", "repo": "o/r", "base_commit": late}]
    counted = population.write(tmp_path, result, questions)
    kept = json.loads(result.read_text())
    assert [row["main_language"] for row in kept["instances"]] == ["javascript", "typescript"]     # documentation is not counted
    assert kept["instances"][0]["scores"] == {} and kept["summary"] == {"instances": 2}           # nothing else is touched
    assert counted == {"o/r": {"javascript": 1, "typescript": 1}}


def test_a_scored_question_that_the_set_does_not_hold_ends_the_reading(tmp_path):
    import pytest

    result = tmp_path / "result.json"
    before = json.dumps({"summary": {}, "instances": [{"instance": "gone-1", "repo": "o/r"}]})
    result.write_text(before)
    with pytest.raises(SystemExit, match="gone-1"):
        population.write(tmp_path, result, [])
    assert result.read_text() == before
