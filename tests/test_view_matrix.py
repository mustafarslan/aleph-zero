"""M1's gate in one place: whatever happens to the worktree, the next query reflects it.

Each case changes the repository in one way and then asks the engine, with no step in
between. The same engine instance is kept across a case, as the MCP server keeps one.
"""
import multiprocessing
import os

from aleph_zero.engine import Engine


def found(engine, word, **options):
    return [hit.path for hit in engine.search(word, **options).hits]


def test_an_edit_is_seen_staged_or_not_and_the_old_text_is_gone(repo):
    engine = Engine(repo.root)
    assert found(engine, "render_invoice") != []
    repo.write("src/billing/invoice.py", "def render_receipt(order):\n    return 'kiwiword'\n")
    assert found(engine, "kiwiword") == ["src/billing/invoice.py"]
    assert all("Invoice total" not in hit.text for hit in engine.search("Invoice total").hits)
    repo.git("add", "-A")                                   # staged, not committed
    assert found(engine, "kiwiword") == ["src/billing/invoice.py"]
    repo.write("src/billing/invoice.py", "def render_receipt(order):\n    return 'emuword'\n")  # staged and edited again
    assert found(engine, "emuword") == ["src/billing/invoice.py"] and found(engine, "kiwiword") == []


def test_untracked_deleted_and_renamed_files(repo):
    engine = Engine(repo.root)
    repo.write("notes/todo.md", "# Todo\n\nShip the okapi_feature.\n")
    assert found(engine, "okapi_feature") == ["notes/todo.md"]
    os.remove(repo.root / "notes/todo.md")
    assert found(engine, "okapi_feature") == []
    os.remove(repo.root / "docs/guide.md")                  # a tracked file deleted, not staged
    assert "docs/guide.md" not in found(engine, "tokens expire")
    repo.git("checkout", "--", "docs/guide.md")
    assert "docs/guide.md" in found(engine, "tokens expire")
    repo.git("mv", "src/billing/invoice.py", "src/billing/bills.py")
    assert found(engine, "render_invoice")[0] == "src/billing/bills.py"
    os.rename(repo.root / "src/auth/token_store.py", repo.root / "src/auth/tokens.py")  # renamed outside git
    assert found(engine, "refresh_token")[0] == "src/auth/tokens.py"


def test_branch_switch_detached_head_and_stash(repo):
    engine = Engine(repo.root)
    first = repo.git("rev-parse", "HEAD").strip()
    repo.git("checkout", "-q", "-b", "feature")
    repo.write("src/feature.py", "def quokka_feature():\n    return 1\n")
    repo.commit("feature")
    assert found(engine, "quokka_feature") == ["src/feature.py"]
    repo.git("checkout", "-q", "main")
    assert found(engine, "quokka_feature") == []
    repo.git("checkout", "-q", "--detach", "feature")
    assert found(engine, "quokka_feature") == ["src/feature.py"]
    repo.git("checkout", "-q", "--detach", first)
    assert found(engine, "quokka_feature") == []
    repo.git("checkout", "-q", "main")
    repo.write("src/billing/invoice.py", "def render_invoice(order):\n    return 'bilbyword'\n")
    assert found(engine, "bilbyword") == ["src/billing/invoice.py"]
    repo.git("stash", "-q")
    assert found(engine, "bilbyword") == []
    repo.git("stash", "pop", "-q")
    assert found(engine, "bilbyword") == ["src/billing/invoice.py"]


def test_ignore_rules_take_effect_at_once(repo):
    engine = Engine(repo.root)
    repo.write("build/generated.py", "def numbat_generated():\n    return 1\n")
    assert found(engine, "numbat_generated") == ["build/generated.py"]
    repo.write(".gitignore", "build/\n")
    assert found(engine, "numbat_generated") == []
    repo.write(".gitignore", "")
    assert found(engine, "numbat_generated") == ["build/generated.py"]


def test_two_worktrees_and_a_commit_made_in_the_other_one(repo, tmp_path):
    other = tmp_path / "second"
    repo.git("worktree", "add", "-q", "-b", "second", str(other))
    engine = Engine(repo.root)
    (other / "src/second_only.py").write_text("def potoroo_second():\n    return 1\n")
    assert found(engine, "potoroo_second", cwd=other) == ["src/second_only.py"]
    assert found(engine, "potoroo_second") == []
    repo.write("src/first_only.py", "def bandicoot_first():\n    return 1\n")
    assert found(engine, "bandicoot_first") == ["src/first_only.py"]
    assert found(engine, "bandicoot_first", cwd=other) == []


def _session(root, word, queue):
    from pathlib import Path

    engine = Engine(Path(root))
    results = [bool(engine.search(word).hits) for _ in range(20)]
    queue.put((word, all(results), engine.status()["pending"]))


def test_concurrent_sessions_in_two_worktrees_do_not_disturb_each_other(repo, tmp_path):
    other = tmp_path / "second"
    repo.git("worktree", "add", "-q", "-b", "second", str(other))
    for i in range(60):
        repo.write(f"pkg/first_{i}.py", f"def first_function_{i}():\n    return 'wallaroo_first'\n")
        (other / "pkg").mkdir(exist_ok=True)
        (other / f"pkg/second_{i}.py").write_text(f"def second_function_{i}():\n    return 'pademelon_second'\n")
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_session, args=(str(repo.root), "wallaroo_first", queue)),
               context.Process(target=_session, args=(str(other), "pademelon_second", queue))]
    for worker in workers:
        worker.start()
    results = sorted(queue.get(timeout=120) for _ in workers)
    for worker in workers:
        worker.join()
        assert worker.exitcode == 0
    assert results == [("pademelon_second", True, 0), ("wallaroo_first", True, 0)]
    engine = Engine(repo.root)
    assert found(engine, "pademelon_second") == [] and len(found(engine, "pademelon_second", cwd=other, k=5)) == 5
