"""Setting Aleph up the first time (ADR-013): the checklist is a function of what is found and what was answered."""
from dataclasses import replace

from aleph_zero import firstrun, settings

PYTHON = firstrun.Found(claude=True, plugin_dir="/src/aleph/plugin", worker=False, language="python", verdict=False,
                        verdict_why="the benchmark for python says vectors do not help enough", chunks=1200, with_vector=0,
                        sources=0, untracked_cache=False, repository=True)
CPP = replace(PYTHON, language="cpp", verdict=True, verdict_why="the benchmark for cpp says vectors help")


def _by_key(steps):
    return {step.key: step for step in steps}


def test_a_fresh_python_repository_has_five_open_steps_and_none_that_costs_is_suggested():
    steps = firstrun.plan(PYTHON, repo={}, machine={})
    # Sources before vectors: what is linked is embedded with the rest, so it is asked for first.
    assert [step.key for step in steps] == ["claude", "worker", "memory", "sources", "vectors", "untracked_cache"]
    assert {step.state for step in steps} == {"open"}
    by = _by_key(steps)
    assert by["claude"].suggested and "claude --plugin-dir /src/aleph/plugin" in by["claude"].command
    assert not by["worker"].suggested and "310 MB" in by["worker"].cost and by["worker"].scope == "machine"
    assert not by["vectors"].suggested and "do not help enough" in by["vectors"].detail and by["vectors"].scope == "repository"
    assert not by["sources"].suggested and not by["untracked_cache"].suggested
    assert by["untracked_cache"].command == "git update-index --untracked-cache"       # never `git config`: that is the person's to run


def test_where_the_verdict_is_for_vectors_the_worker_and_the_vectors_are_suggested_with_what_they_cost():
    by = _by_key(firstrun.plan(CPP, repo={}, machine={}))
    assert by["worker"].suggested and by["vectors"].suggested
    assert "1,200 chunks" in by["vectors"].cost and "interrupted" in by["vectors"].cost


def test_a_person_who_asked_for_search_by_meaning_in_a_python_repository_is_offered_what_it_needs():
    by = _by_key(firstrun.plan(PYTHON, repo={"dense": True}, machine={}))
    assert by["worker"].suggested and by["vectors"].suggested and by["vectors"].state == "open"
    done = _by_key(firstrun.plan(replace(PYTHON, worker=True, with_vector=1200), repo={"dense": True}, machine={}))
    assert done["worker"].state == "done" and done["vectors"].state == "done"


def test_what_is_found_done_is_done_and_what_was_declined_is_not_open_again():
    found = replace(CPP, worker=True, with_vector=1200, sources=2, untracked_cache=True, memory=True)
    by = _by_key(firstrun.plan(found, repo={}, machine={"setup": {"claude": "done"}}))
    assert {step.state for step in by.values()} == {"done"} and "2 linked" in by["sources"].detail
    declined = _by_key(firstrun.plan(CPP, repo={"setup": {"sources": "declined", "untracked_cache": "declined"}, "dense": False},
                                     machine={"setup": {"worker": "declined", "claude": "declined"}}))
    assert {step.state for step in declined.values()} == {"declined"}
    assert firstrun.open_steps(firstrun.plan(CPP, repo={}, machine={})) == ["claude", "worker", "memory", "sources", "vectors", "untracked_cache"]
    assert firstrun.open_steps(list(declined.values())) == []


def test_vectors_made_for_a_verdict_against_are_not_called_done_until_the_repository_says_to_use_them():
    made = replace(PYTHON, worker=True, with_vector=1200)
    assert _by_key(firstrun.plan(made, repo={}, machine={}))["vectors"].state == "open"       # made but not used: still a question
    assert _by_key(firstrun.plan(made, repo={"dense": True}, machine={}))["vectors"].state == "done"


def test_without_the_worker_and_with_a_no_to_it_vectors_are_not_a_question_any_more():
    by = _by_key(firstrun.plan(CPP, repo={}, machine={"setup": {"worker": "declined"}}))
    assert by["worker"].state == "declined" and by["vectors"].state == "declined" and "worker" in by["vectors"].detail
    installed = _by_key(firstrun.plan(replace(CPP, worker=True), repo={}, machine={"setup": {"worker": "declined"}}))
    assert installed["vectors"].state == "open"            # the worker is there after all: the question stands


def test_a_directory_that_is_not_a_repository_is_not_offered_what_needs_one():
    steps = firstrun.plan(replace(PYTHON, repository=False), repo={}, machine={})
    assert [step.key for step in steps] == ["claude", "worker", "memory", "vectors"]     # memory is the machine's


def test_answers_are_kept_in_a_file_and_a_damaged_file_is_no_answers(tmp_path):
    path = tmp_path / "state" / "settings.json"
    assert settings.read(path) == {}
    settings.answer(path, "worker", "declined")
    settings.change(path, dense=True)
    assert settings.read(path) == {"setup": {"worker": "declined"}, "dense": True}
    settings.answer(path, "worker", None)                 # asked again and left open
    settings.change(path, dense=None)
    assert settings.read(path) == {"setup": {}}
    path.write_text("{ not json")
    assert settings.read(path) == {}
    settings.answer(path, "claude", "done")               # and it can be written again
    assert settings.read(path) == {"setup": {"claude": "done"}}


def test_the_machine_s_answers_live_in_the_cache_directory_beside_the_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert settings.machine_file() == tmp_path / "aleph-zero" / "settings.json"


def test_a_repository_s_answers_live_beside_its_index(repo):
    from aleph_zero import gitio
    from aleph_zero.store import index_file

    common = gitio.common_dir(repo.root)
    assert settings.repo_file(common) == index_file(common).parent / "settings.json"


def test_the_cost_of_vectors_is_said_in_plain_numbers_and_the_reason_for_a_no_comes_first():
    one = _by_key(firstrun.plan(replace(CPP, chunks=1), repo={}, machine={}))["vectors"]
    assert "for 1 chunk;" in one.cost and "1 chunks" not in one.cost and "0 of 1 chunk with a vector" in one.detail
    declined = _by_key(firstrun.plan(CPP, repo={}, machine={"setup": {"worker": "declined"}}))["vectors"]
    assert declined.detail.startswith("needs the embedding worker, which was declined")      # the first thing read, not the last


# ---- from the review of the branch (2026-10-13) ---------------------------------------------------


def test_a_choice_that_is_not_yes_or_no_is_no_choice():
    assert firstrun.plan(PYTHON, repo={"dense": "no"}, machine={}) == firstrun.plan(PYTHON, repo={}, machine={})
    assert firstrun.plan(CPP, repo={"dense": 1}, machine={}) == firstrun.plan(CPP, repo={}, machine={})


def test_vectors_are_done_once_chosen_and_begun_and_an_edit_does_not_make_them_a_question_again():
    edited = replace(CPP, worker=True, chunks=1210, with_vector=1200)          # ten chunks written since
    step = _by_key(firstrun.plan(edited, repo={}, machine={}))["vectors"]
    assert step.state == "done" and "1,200 of 1,210" in step.detail and "aleph embed run" in step.detail
    nothing_yet = _by_key(firstrun.plan(replace(CPP, worker=True, chunks=0), repo={}, machine={}))["vectors"]
    assert nothing_yet.state == "done" and nothing_yet.cost == ""            # an empty repository: nothing to make, nothing to ask
    never_made = _by_key(firstrun.plan(replace(CPP, worker=True), repo={}, machine={}))["vectors"]
    assert never_made.state == "open"


def test_what_the_list_says_is_true_of_the_case():
    wanted = _by_key(firstrun.plan(CPP, repo={}, machine={}))
    assert "search by meaning needs it" in wanted["worker"].detail             # not "nothing needs it"
    assert "nothing needs it" in _by_key(firstrun.plan(PYTHON, repo={}, machine={}))["worker"].detail
    assert "worktree" in wanted["untracked_cache"].title                       # git's index, and so its cache, is a worktree's
    claude = wanted["claude"]
    assert "claude mcp add --scope user aleph-zero -- aleph serve" in claude.command      # for every project: the answer is the machine's
    assert "one session" in claude.command


def test_two_writers_of_one_file_both_keep_their_answer(tmp_path):
    import threading

    path = tmp_path / "settings.json"
    writers = [threading.Thread(target=settings.answer, args=(path, f"step{n}", "done")) for n in range(24)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert len(settings.read(path)["setup"]) == 24


def test_no_hint_once_anything_was_answered_however_it_was_answered(repo):
    from aleph_zero import gitio

    kept = settings.repo_file(gitio.common_dir(repo.root))
    settings.answer(kept, "sources", None)                 # answered by a flag: the file holds an empty list of answers
    assert settings.read(kept) == {"setup": {}}
    assert firstrun.hint(repo.root) is None


def test_git_s_untracked_cache_is_suggested_where_the_worktree_is_large_enough_for_it_to_pay():
    # The owner said yes (2026-10-14). Measured: vscode (5,000 files) 36 -> 13 ms a question, babel (27,000) 288 -> 60;
    # this repository (235 files) 7.2 -> 7.3. Still a step a person answers, in the form that changes no configuration.
    small = replace(CPP, files=235)
    large = replace(CPP, files=firstrun.UNTRACKED_CACHE_FROM)
    assert not _by_key(firstrun.plan(small, repo={}, machine={}))["untracked_cache"].suggested
    step = _by_key(firstrun.plan(large, repo={}, machine={}))["untracked_cache"]
    assert step.suggested and step.command == "git update-index --untracked-cache"
    assert not _by_key(firstrun.plan(replace(large, untracked_cache=True), repo={}, machine={}))["untracked_cache"].suggested
    assert firstrun.look({"files_in_view": 12_345, "untracked_cache": False}).files == 12_345


def test_the_files_counted_for_git_s_untracked_cache_are_the_worktree_s_own_not_its_linked_sources():
    # Git's look at the worktree is what the cache speeds up; a linked source's files are another repository's.
    status = {"files_in_view": 5_050, "untracked_cache": False, "sources": [{"name": "docs", "kind": "path", "files": 5_000}]}
    assert firstrun.look(status).files == 50


def test_with_the_plugin_installed_allowing_its_tools_is_asked_after_connecting_and_suggested():
    assert "allow" not in _by_key(firstrun.plan(PYTHON, repo={}, machine={}))          # no plugin: nothing to allow
    steps = firstrun.plan(replace(PYTHON, plugin=True), repo={}, machine={})
    assert [step.key for step in steps][:3] == ["claude", "allow", "worker"]
    allow = _by_key(steps)["allow"]
    assert allow.state == "open" and allow.suggested and allow.scope == "machine"
    assert "mcp__plugin_aleph-zero_aleph__*" in allow.cost and "nothing else there is changed" in allow.cost
    assert allow.command.endswith("setup --allow-tools")
    assert _by_key(firstrun.plan(replace(PYTHON, plugin=True, allowed=True), repo={}, machine={}))["allow"].state == "done"


def test_allowing_the_tools_adds_one_rule_and_keeps_everything_else(tmp_path, monkeypatch):
    import json

    import pytest

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert not firstrun.tools_allowed()
    assert "added" in firstrun.allow_tools()                                              # no file yet: made
    assert firstrun.tools_allowed()
    path = tmp_path / "claude" / "settings.json"
    path.write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls:*)"], "deny": ["Read(./.env)"]}, "x": [1]}))
    firstrun.allow_tools()
    kept = json.loads(path.read_text())
    assert kept == {"model": "opus", "permissions": {"allow": ["Bash(ls:*)", "mcp__plugin_aleph-zero_aleph__*"],
                                                     "deny": ["Read(./.env)"]}, "x": [1]}
    assert firstrun.allow_tools().startswith("already allowed")
    assert json.loads(path.read_text()) == kept                                           # asked twice, written once
    path.write_text("{ not json")
    with pytest.raises(firstrun.SettingsError, match="by hand"):
        firstrun.allow_tools()
    assert path.read_text() == "{ not json"                                               # a file it cannot read is left
