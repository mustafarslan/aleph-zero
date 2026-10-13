import os

import pytest

from aleph_zero import filters, gitio
from aleph_zero.view import build_view


def test_clean_view_is_heads_tree(repo):
    view = build_view(repo.root)
    assert view.files == gitio.ls_tree(repo.root, gitio.head_tree(repo.root))
    assert view.dirty == frozenset()


def test_edit_changes_the_blob_and_marks_the_path_dirty(repo):
    before = build_view(repo.root)
    repo.write("src/billing/invoice.py", "def render_invoice(order):\n    return 'changed'\n")
    after = build_view(repo.root)
    assert after.files["src/billing/invoice.py"] != before.files["src/billing/invoice.py"]
    assert after.dirty == frozenset({"src/billing/invoice.py"})
    assert after.files["docs/guide.md"] == before.files["docs/guide.md"]


def test_deleted_file_leaves_the_view_whether_staged_or_not(repo):
    os.remove(repo.root / "docs/guide.md")
    assert "docs/guide.md" not in build_view(repo.root).files
    repo.git("rm", "-q", "--cached", "docs/guide.md")
    assert "docs/guide.md" not in build_view(repo.root).files


def test_untracked_file_joins_the_view(repo):
    repo.write("scratch/new_module.py", "x = 1\n")
    view = build_view(repo.root)
    assert view.files["scratch/new_module.py"] == gitio.hash_blob(b"x = 1\n")
    assert "scratch/new_module.py" in view.dirty


def test_rename_keeps_the_blob_and_drops_the_old_path(repo):
    before = build_view(repo.root)
    repo.git("mv", "src/billing/invoice.py", "src/billing/bill.py")
    after = build_view(repo.root)
    assert "src/billing/invoice.py" not in after.files
    assert after.files["src/billing/bill.py"] == before.files["src/billing/invoice.py"]


def test_detached_head_and_second_worktree_each_have_their_own_view(repo, tmp_path):
    first = repo.git("rev-parse", "HEAD").strip()
    repo.write("src/billing/invoice.py", "v2 = True\n")
    repo.commit("second")
    other = tmp_path / "other"
    repo.git("worktree", "add", "-q", "--detach", str(other), first)
    assert build_view(other).files != build_view(repo.root).files
    assert build_view(other).files["src/billing/invoice.py"] == repo.git("rev-parse", f"{first}:src/billing/invoice.py").strip()


def test_repository_without_commits_has_a_view_of_its_untracked_files(empty_repo):
    empty_repo.write("a.py", "print('hi')\n")
    view = build_view(empty_repo.root)
    assert view.tree is None
    assert set(view.files) == {"a.py"}


def test_fingerprint_is_stable_and_changes_with_content(repo):
    one = build_view(repo.root).fingerprint
    assert build_view(repo.root).fingerprint == one
    repo.write("docs/guide.md", "# Different\n")
    assert build_view(repo.root).fingerprint != one


def test_oversized_dirty_file_is_left_out(repo):
    repo.write("big.txt", "x" * (filters.MAX_BYTES + 1))
    assert "big.txt" not in build_view(repo.root).files


def test_paths_for_lists_every_path_with_the_same_content(repo):
    repo.write("src/billing/copy.py", (repo.root / "src/billing/invoice.py").read_text())
    view = build_view(repo.root)
    assert view.paths_for(view.files["src/billing/invoice.py"]) == ["src/billing/copy.py", "src/billing/invoice.py"]


def test_read_takes_dirty_content_from_disk_and_clean_content_from_git(repo):
    repo.write("src/billing/invoice.py", "dirty = True\n")
    view = build_view(repo.root)
    with gitio.Readers() as reader:
        assert view.read("src/billing/invoice.py", reader) == b"dirty = True\n"
        assert view.read("docs/guide.md", reader).startswith(b"# Guide")


def test_a_submodule_entry_is_left_out_without_error(repo):
    commit = repo.git("rev-parse", "HEAD").strip()
    repo.git("update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
    repo.git("commit", "-q", "-m", "add gitlink")
    view = build_view(repo.root)
    assert "vendor/lib" not in view.files
    assert "docs/guide.md" in view.files


def test_an_unreadable_file_is_left_out_of_the_view(repo):
    secret = repo.write("secret.py", "x = 1\n")
    secret.chmod(0)
    try:
        view = build_view(repo.root)
    finally:
        secret.chmod(0o644)
    assert "secret.py" not in view.files


def test_read_refuses_dirty_content_that_changed_after_the_view_was_built(repo):
    repo.write("src/billing/invoice.py", "first = 1\n")
    view = build_view(repo.root)
    repo.write("src/billing/invoice.py", "second = 2\n")
    with gitio.Readers() as reader:
        assert view.read("src/billing/invoice.py", reader) is None


def test_a_case_only_rename_leaves_one_name_in_the_view(repo):
    if not (repo.root / "DOCS").exists():
        pytest.skip("this file system is case-sensitive")
    repo.git("mv", "docs/guide.md", "docs/Guide.md")
    view = build_view(repo.root)
    assert "docs/Guide.md" in view.files and "docs/guide.md" not in view.files


def test_git_is_asked_about_the_changes_while_it_is_asked_about_the_repository(repo, monkeypatch):
    # Two git processes for every question, one after the other: the repository's facts took 9 to 11 ms of each
    # (a quarter of a question on a small repository). The second is started before the first is waited for.
    import subprocess

    from aleph_zero import gitio

    events: list[str] = []
    real_popen = subprocess.Popen

    class Watched(real_popen):
        def __init__(self, command, *args, **kwargs):
            self._what = next((word for word in command if word in ("status", "rev-parse")), None)
            if self._what:
                events.append(f"{self._what} starts")
            super().__init__(command, *args, **kwargs)

        def communicate(self, *args, **kwargs):
            out = super().communicate(*args, **kwargs)
            if self._what:
                events.append(f"{self._what} ends")
            return out

    monkeypatch.setattr(gitio.subprocess, "Popen", Watched)
    repo.write("src/billing/invoice.py", "def render_invoice(order):\n    return 'changed'\n")
    view = build_view(repo.root)
    assert events.index("status starts") < events.index("rev-parse ends"), events
    assert view.dirty == frozenset({"src/billing/invoice.py"})


def test_a_view_asked_for_from_a_sub_directory_holds_the_same_files_untracked_directories_too(repo):
    # git answers `status` with paths from the repository's top wherever it is asked; what is listed afterwards
    # (the files of a wholly untracked directory) must be asked for from the top too.
    repo.write("fresh/deep/one.py", "def one():\n    return 1\n")
    repo.write("fresh/two.py", "def two():\n    return 2\n")
    repo.write("src/auth/edited_here.py", "def here():\n    return 0\n")
    top = build_view(repo.root)
    below = build_view(repo.root / "src" / "auth")
    assert below.files == top.files and below.dirty == top.dirty
    assert {"fresh/deep/one.py", "fresh/two.py", "src/auth/edited_here.py"} <= set(below.files)


def test_a_directory_that_is_no_repository_leaves_no_git_process_behind_and_is_read_as_it_is(tmp_path):
    import subprocess

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "notes.py").write_text("def note():\n    return 1\n")
    view = build_view(plain)
    assert "notes.py" in view.files and view.plain
    running = subprocess.run(["pgrep", "-f", f"git -C {plain} status"], capture_output=True, text=True).stdout.split()
    assert running == []
