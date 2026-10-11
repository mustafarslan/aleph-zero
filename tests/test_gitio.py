import os

import pytest

from aleph_zero import gitio


def test_hash_blob_matches_git(repo):
    data = (repo.root / "src/billing/invoice.py").read_bytes()
    assert gitio.hash_blob(data) == repo.git("hash-object", "src/billing/invoice.py").strip()


def test_ls_tree_lists_regular_files_and_leaves_out_symlinks(repo):
    os.symlink("invoice.py", repo.root / "src/billing/link.py")
    repo.commit("symlink")
    files = gitio.ls_tree(repo.root, gitio.head_tree(repo.root))
    assert set(files) == {"src/auth/token_store.py", "src/billing/invoice.py", "docs/guide.md"}
    assert files["docs/guide.md"] == repo.git("rev-parse", "HEAD:docs/guide.md").strip()


def test_status_reports_modified_deleted_untracked_and_both_names_of_a_rename(repo):
    repo.write("src/billing/invoice.py", "changed = True\n")
    os.remove(repo.root / "docs/guide.md")
    repo.git("mv", "src/auth/token_store.py", "src/auth/tokens.py")
    repo.write("notes/with space.txt", "untracked\n")
    assert set(gitio.status(repo.root)) == {
        "src/billing/invoice.py", "docs/guide.md",
        "src/auth/tokens.py", "src/auth/token_store.py", "notes/with space.txt",
    }


def test_status_is_empty_for_a_clean_tree(repo):
    assert gitio.status(repo.root) == []


def test_blob_reader_returns_content_and_none_for_a_missing_object(repo):
    oid = repo.git("rev-parse", "HEAD:src/billing/invoice.py").strip()
    with gitio.BlobReader(repo.root) as reader:
        assert reader.read(oid) == (repo.root / "src/billing/invoice.py").read_bytes()
        assert reader.read("0" * 40) is None
        assert reader.read(oid) is not None  # still usable after a miss


def test_head_tree_is_none_before_the_first_commit(empty_repo):
    assert gitio.head_tree(empty_repo.root) is None


def test_linked_worktree_shares_the_common_dir(repo, tmp_path):
    other = tmp_path / "other"
    repo.git("worktree", "add", "-q", "--detach", str(other))
    assert gitio.common_dir(other) == gitio.common_dir(repo.root)
    assert gitio.toplevel(other) != gitio.toplevel(repo.root)


def test_outside_a_repository_raises_a_git_error(tmp_path):
    with pytest.raises(gitio.GitError):
        gitio.toplevel(tmp_path)


def test_repo_info_gets_root_format_common_dir_and_tree_in_one_call(repo):
    info = gitio.repo_info(repo.root / "src")
    assert info.root == gitio.toplevel(repo.root)
    assert info.object_format == "sha1"
    assert info.common_dir == gitio.common_dir(repo.root)
    assert info.tree == gitio.head_tree(repo.root)


def test_repo_info_has_no_tree_before_the_first_commit(empty_repo):
    info = gitio.repo_info(empty_repo.root)
    assert info.tree is None and info.root == gitio.toplevel(empty_repo.root)


def test_repo_info_outside_a_repository_raises(tmp_path):
    with pytest.raises(gitio.GitError):
        gitio.repo_info(tmp_path)
