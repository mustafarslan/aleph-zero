"""Which decision governs a change: decision records ranked as whole documents for a task."""
import json

from aleph_zero import cli
from aleph_zero.engine import Engine

BUS = ("# 12. Use an event bus\n\nDate: 2026-01-02\n\n## Status\n\nAccepted\n\n## Context\n\nServices call each other directly and retries are everywhere.\n\n"
       "## Decision\n\nWe publish domain events on a bus. Consumers subscribe; delivery is at least once, so handlers are idempotent.\n")
LOADER = ("# 13. One parser for decisions\n\n## Status\n\nSuperseded by ADR-14\n\n## Decision\n\n`load_decisions` is the only reader of the ADR directory. "
          "Nothing else parses front matter.\n")
STORAGE = ("---\nid: ADR-0014\ntitle: Postgres for everything\nstatus: accepted\ntags: [storage, database]\n---\n\n"
           "We keep relational data in Postgres and do not add a second database for queues.\n")


def project(repo):
    repo.write("docs/adr/0012-use-an-event-bus.md", BUS)
    repo.write("docs/adr/0013-one-parser.md", LOADER)
    repo.write("docs/adr/0014-postgres.md", STORAGE)
    repo.write("docs/adr/README.md", "# Decisions\n\nAn index of the event bus, the parser and Postgres decisions.\n")
    repo.write("docs/guide/0001-not-a-decision.md", "# 1. A guide that happens to be numbered\n\nevent bus event bus\n")
    repo.write("docs/decisions/ADR-020-feature-flags.md", "# ADR-020: Feature flags are data\n\nStatus: proposed\n\nFlags live in the database, not in code.\n")
    return Engine(repo.root)


def test_the_decisions_of_a_view_are_the_numbered_records_with_their_number_title_and_status(repo):
    found = {hit.number: (hit.path, hit.title, hit.status) for hit in project(repo).decisions("", k=50).decisions}
    assert found == {
        12: ("docs/adr/0012-use-an-event-bus.md", "Use an event bus", "Accepted"),
        13: ("docs/adr/0013-one-parser.md", "One parser for decisions", "Superseded by ADR-14"),
        14: ("docs/adr/0014-postgres.md", "Postgres for everything", "accepted"),
        20: ("docs/decisions/ADR-020-feature-flags.md", "Feature flags are data", "proposed"),
    }           # not the README, and not a numbered file outside a directory of decisions


def test_a_task_is_ranked_against_whole_documents_with_all_its_words(repo):
    engine = project(repo)
    task = "Make the order handler idempotent, since the bus may deliver an event twice after retries"
    assert [hit.number for hit in engine.decisions(task).decisions][:1] == [12]
    assert [hit.number for hit in engine.decisions("fix the ADR loader so that it reads front matter once").decisions][:1] == [13]  # load_decisions
    assert [hit.number for hit in engine.decisions("should job queues get a second database, or stay in Postgres").decisions][0] == 14
    assert engine.decisions("zzz qqq").decisions == []                  # shares no word with any decision: none, not all of them


def test_an_edited_or_removed_decision_is_ranked_as_it_is_now(repo):
    engine = project(repo)
    assert engine.decisions("kafka partitions").decisions == []
    repo.write("docs/adr/0012-use-an-event-bus.md", BUS + "\nThe bus is Kafka; one partition per aggregate.\n")
    assert [hit.number for hit in engine.decisions("kafka partitions").decisions] == [12]
    (repo.root / "docs/adr/0012-use-an-event-bus.md").unlink()
    assert engine.decisions("kafka partitions").decisions == []


def test_the_answer_is_short_and_says_when_there_is_nothing_to_rank(repo, capsys):
    from aleph_zero import decisions

    engine = project(repo)
    text = decisions.text(engine.decisions("publish events on the bus"))
    assert text.splitlines()[0] == "docs/adr/0012-use-an-event-bus.md  ADR 12  Use an event bus  [Accepted]"
    assert len(text) < 600
    assert cli.main(["--cwd", str(repo.root), "decisions", "publish events on the bus", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["decisions"][0]["number"] == 12
    for path in list((repo.root / "docs").rglob("*.md")):
        path.unlink()
    assert "no decision records" in decisions.text(Engine(repo.root).decisions("anything"))


def test_ranking_is_the_same_every_time_also_between_equal_scores(repo):
    repo.write("docs/adr/0002-b.md", "# 2. Second\n\nthe same words here\n")
    repo.write("docs/adr/0001-a.md", "# 1. First\n\nthe same words here\n")
    assert [hit.number for hit in Engine(repo.root).decisions("same words").decisions] == [1, 2]


def test_a_file_named_as_a_decision_is_one_wherever_it_lies(repo):
    repo.write("docs/adr-004-blue-green-migration.md", "# ADR-004: Blue-green migration\n\n**Status:** accepted\n\nTwo environments, one switch.\n")
    repo.write("docs/0007-release-notes.md", "# 7. Release notes\n\nNot a decision: numbered, but neither named ADR nor in a directory of decisions.\n")
    found = [(hit.path, hit.number, hit.title, hit.status) for hit in Engine(repo.root).decisions("", k=10).decisions]
    assert found == [("docs/adr-004-blue-green-migration.md", 4, "Blue-green migration", "accepted")]


ADR_WITH_SCOPE = ('---\nid: ADR-0003\ntitle: Hooks enforce, the server informs\nstatus: accepted\n'
                  'tags: [hooks]\napplies-to: ["src/app/cli.py", "src/app/rules/*"]\n---\n\n# ADR-0003\n\nHooks enforce.\n')
ADR_BLOCK_SCOPE = '---\nid: ADR-0004\ntitle: Metrics are computed once\nstatus: proposed\napplies-to:\n  - "src/app/metrics/*"\n---\n\nOnce.\n'
LAYERS = ('layers:\n  surfaces: ["src/app/cli.py"]\n  analysis: ["src/app/metrics/*", "src/app/rules/*"]\n  foundation: ["src/app/config.py"]\n'
          'contracts:\n  - name: outside-in\n    kind: layered\n    order: [surfaces, analysis, foundation]\n')


def test_which_records_govern_a_file_and_which_layer_it_is_in(repo):
    from aleph_zero import decisions
    from aleph_zero.engine import Engine

    repo.write("docs/adr/0003-hooks-enforce.md", ADR_WITH_SCOPE)
    repo.write("docs/adr/0004-metrics-once.md", ADR_BLOCK_SCOPE)
    repo.write("docs/adr/0005-no-scope.md", "# 5. Nothing in particular\n\n## Status\n\nAccepted\n")
    repo.write("oxn.yaml", LAYERS)
    repo.write("src/app/rules/size.py", "LIMIT = 1\n")
    engine = Engine(repo.root)
    answer = engine.decisions("", path="src/app/rules/size.py")
    assert [(hit.number, hit.status) for hit in answer.governing] == [(3, "accepted")]
    assert answer.place == ["layer analysis (contract outside-in): may depend on foundation; not on surfaces"]
    assert decisions.text(answer).split("\n")[:3] == [
        "src/app/rules/size.py is governed by:",
        "docs/adr/0003-hooks-enforce.md  ADR 3  Hooks enforce, the server informs  [accepted]",
        "layer analysis (contract outside-in): may depend on foundation; not on surfaces"]
    assert [hit.number for hit in engine.decisions("", path=str(repo.root / "src/app/metrics/x.py")).governing] == [4]
    nowhere = engine.decisions("hooks", path="README.md")
    assert nowhere.governing == [] and nowhere.place == ["in no layer of oxn.yaml"]
    assert decisions.text(nowhere).startswith("README.md: no decision record names it in its applies-to\nin no layer of oxn.yaml\n\ndocs/adr/0003")
    assert engine.decisions("hooks").place == [] and "governed" not in decisions.text(engine.decisions("hooks"))


def test_a_record_s_scope_is_not_ranked(repo):
    from aleph_zero import decisions

    with_scope = decisions.parse("docs/adr/0003-x.md", 3, ADR_WITH_SCOPE)
    assert with_scope.applies_to == ("src/app/cli.py", "src/app/rules/*") and "src/app/cli.py" not in with_scope.text


def test_a_file_two_layers_name_is_in_the_first_as_oxn_has_it():
    from aleph_zero import architecture

    layers, contracts = architecture.read(
        "layers:\n  orchestration: [\"src/graph/indexer.py\"]\n  graph: [\"src/graph/*\"]\n"
        "contracts:\n  - name: c\n    kind: layered\n    order: [orchestration, graph]\n")
    assert architecture.place("src/graph/indexer.py", layers, contracts) == ["layer orchestration (contract c): may depend on graph"]
    assert architecture.place("src/graph/store.py", layers, contracts) == ["layer graph (contract c): may depend on no other layer; not on orchestration"]


def test_words_that_differ_only_in_their_ending_are_one_word():
    from aleph_zero.decisions import stem

    for same in (("cache", "caches", "caching", "cached"), ("policy", "policies"), ("class", "classes"), ("bus", "buses"),
                 ("status", "statuses"), ("need", "needs", "needed"), ("add", "adds", "added", "adding"), ("map", "mapped", "mapping"),
                 ("store", "stores", "stored", "storing"), ("case", "cases"), ("index", "indexes", "indexed", "indexing"),
                 ("call", "calls", "called", "calling"), ("embed", "embedded", "embedding"), ("service", "services")):
        assert len({stem(word) for word in same}) == 1, same
    for kept in ("string", "thing", "redis", "status", "bus", "is", "its", "http2", "sqs", "less"):
        assert stem(kept) == kept, kept
    assert stem("string") != stem("str") and stem("bus") != stem("bu") and stem("analysis") == "analysis"


def test_a_ranker_that_stems_finds_the_record_by_another_form_of_its_word():
    from aleph_zero import decisions

    records = [decisions.parse("docs/adr/0001-a.md", 1, "# 1. Responses are cached at the edge\n\nWe cache.\n"),
               decisions.parse("docs/adr/0002-b.md", 2, "# 2. Queues carry the work\n\nA queue.\n")]
    assert [hit.number for hit in decisions.Records(records, stems=False).rank("caching of a response")] == [2]    # only "a" is shared
    assert [hit.number for hit in decisions.Records(records, stems=True).rank("caching of a response")] == [1, 2]


def test_the_ranking_that_was_measured_is_the_default():
    """The figure on oxn's pairs is of words alone with BM25's usual constants: the switches tried later stay off."""
    from aleph_zero import decisions

    assert (decisions.STEMS, decisions.K1, decisions.B) == (False, 1.5, 0.75)
    records = [decisions.parse(f"docs/adr/000{n}-x.md", n, text) for n, text in enumerate((
        "# 1. Responses are cached at the edge\n\nWe cache responses. Caching is cheap.\n",
        "# 2. Queues carry the work\n\nA queue per kind of work, and a cache of results for the queue.\n" + "More about queues. " * 40,
        "# 3. The cache is invalidated by events\n\nEvents.\n"), 1)]
    task = "invalidate the response cache when an order event arrives"
    default = [(hit.number, hit.score) for hit in decisions.Records(records).rank(task)]
    assert default == [(hit.number, hit.score) for hit in decisions.Records(records, stems=False, b=0.75).rank(task)]
    assert [number for number, _ in default] == [3, 1, 2]
    assert default != [(hit.number, hit.score) for hit in decisions.Records(records, stems=True).rank(task)]
    assert default != [(hit.number, hit.score) for hit in decisions.Records(records, b=0.4).rank(task)]


def test_a_path_outside_the_worktree_is_said_to_be_and_a_half_read_oxn_yaml_says_so(repo):
    from aleph_zero import architecture
    from aleph_zero.engine import Engine

    repo.write("oxn.yaml", "layers:\n  api: [\"src/api/*\"]\n  odd: {include: [\"x/*\"]}\n  nested:\n    include: [\"y/*\"]\ncontracts:\n  - name: c\n    kind: layered\n    order: [api]\n")
    repo.write("src/api/a.py", "x = 1\n")
    engine = Engine(repo.root)
    outside = engine.decisions("", path="/etc/passwd")
    assert outside.place == ["/etc/passwd is not a path of this worktree"] and outside.governing == []
    assert engine.decisions("", path="../elsewhere/x.py").place == ["../elsewhere/x.py is not a path of this worktree"]
    inside = engine.decisions("", path="src/api/a.py")
    assert inside.place[0].startswith("layer api") and inside.place[-1] == "note: 3 lines of oxn.yaml's layers and contracts were not understood"
    assert architecture.unread("layers:\n  api: [\"src/api/*\"]\ncontracts:\n  - name: c\n    kind: layered\n    order: [api]\n") == 0
    assert [layer.name for layer in architecture.read("layers:\n  globs: [\"x/{a,b}/*\", 'it''s/*']\n")[0]] == ["globs"]
    assert architecture.read("layers:\n  globs: [\"x/{a,b}/*\", \"y/*\"]\n")[0][0].patterns == ("x/{a,b}/*", "y/*")     # a comma inside quotes


def test_an_apostrophe_in_a_name_does_not_swallow_the_comment_after_it():
    from aleph_zero import architecture

    text = "layers:\n  api: [\"src/*\"]   # the owner's layer\ncontracts:\n  - name: don't-cycle # checked nightly\n    kind: layered\n    order: [api]\n"
    layers, contracts = architecture.read(text)
    assert [layer.patterns for layer in layers] == [("src/*",)] and [c.name for c in contracts] == ["don't-cycle"]
    assert architecture.read("layers:\n  api: ['it''s/*', \"a # b/*\"]  # two\n")[0][0].patterns == ("it''s/*", "a # b/*")   # a quoted # is not a comment


FIELDED = {
    "docs/adr/0001-a.md": "# 1. Events travel on a message bus\n\n## Context\n\nRetries, retries and retries were discussed; retries matter.\n\n"
                          "## Decision\n\nServices publish events to the bus.\n",
    "docs/adr/0002-b.md": "# 2. Retries are bounded\n\n## Context\n\nThe bus was mentioned once.\n\n## Decision\n\nA request is retried three times.\n",
}


def test_where_a_word_stands_in_a_record_can_be_made_to_count():
    from aleph_zero import decisions

    records = [decisions.parse(path, number, text) for number, (path, text) in enumerate(FIELDED.items(), 1)]
    assert records[0].decision.strip() == "Services publish events to the bus." and "three times" in records[1].decision
    plain = decisions.Records(records)
    assert [hit.number for hit in plain.rank("retries")] == [1, 2]                   # as measured: the record that says it most
    fielded = decisions.Records(records, fields=(3.0, 2.0, 1.0))
    assert [hit.number for hit in fielded.rank("retries")] == [2, 1]                 # its title and its decision are about it
    same = decisions.Records(records, fields=(1.0, 1.0, 1.0))
    assert [(hit.number, hit.score) for hit in same.rank("retries bus events")] == [(hit.number, hit.score) for hit in plain.rank("retries bus events")]


def test_the_first_line_of_a_task_can_be_made_to_count_more_than_the_rest():
    from aleph_zero import decisions

    records = [decisions.parse(path, number, text) for number, (path, text) in enumerate(FIELDED.items(), 1)]
    task = "bounded requests\n\nServices publish events."
    assert [hit.number for hit in decisions.Records(records).rank(task)] == [1, 2]              # the body's many words win
    assert [hit.number for hit in decisions.Records(records, subject=4.0).rank(task)] == [2, 1]  # what the commit is about wins
    assert (decisions.FIELDS, decisions.SUBJECT) == ((1.0, 1.0, 1.0), 1.0)                       # both are off unless asked for


def _three():
    from aleph_zero import decisions

    return [decisions.parse(f"docs/adr/000{n}-x.md", n, text) for n, text in enumerate((
        "# 1. Responses are cached at the edge\n\n## Decision\n\nWe cache responses.\n",
        "# 2. Queues carry the work\n\nA queue per kind of work.\n",
        "# 3. Events invalidate what is kept\n\nNothing here shares a word with the task below.\n"), 1)]


def test_words_and_nearness_are_scaled_and_added_and_a_record_no_word_names_can_still_be_found():
    """The fusion that was chosen on development pairs (bench/eval/decisions_tune.py): for one task, the BM25 scores
    and the cosines are each scaled to 0..1 over all the records and added with a weight."""
    from aleph_zero import decisions

    records = decisions.Records(_three())
    task = "cache responses"
    assert [hit.number for hit in records.rank(task)] == [1]                           # by words: only the first shares one
    near = {"docs/adr/0001-x.md": 0.2, "docs/adr/0002-x.md": 0.1, "docs/adr/0003-x.md": 0.9}
    fused = records.rank(task, nearness=near, a=0.5)
    # words scaled: 1, 0, 0; nearness scaled: 0.125, 0, 1  ->  0.5625, 0, 0.5
    assert [(hit.number, hit.score, hit.sources) for hit in fused] == [(1, 0.5625, ("words", "vectors")), (3, 0.5, ("vectors",)), (2, 0.0, ("vectors",))]
    assert [hit.number for hit in records.rank(task, nearness=near, a=0.3)] == [3, 1, 2]       # nearness counts for more
    assert [hit.number for hit in records.rank(task, nearness={path: 0.4 for path in near}, a=0.5)] == [1, 2, 3]   # equal: the records' own order
    assert [hit.number for hit in records.rank(task, nearness=near, a=0.5, k=1)] == [1]
    assert (decisions.FUSION, decisions.A, decisions.CUT) == ("convex", 0.5, 5000)    # as chosen on development pairs and run once on oxn's


def test_what_is_embedded_for_a_record_is_its_title_its_decision_and_then_its_text():
    from aleph_zero import decisions

    record = _three()[0]
    assert decisions.record_text(record).startswith("Responses are cached at the edge\n\nWe cache responses.")
    assert decisions.record_text(record).endswith(record.text) and decisions.record_text(record).count("We cache responses.") == 2
    long = decisions.parse("docs/adr/0009-x.md", 9, "# 9. Long\n\n" + "word " * 5000)
    assert len(decisions.record_text(long)) == decisions.CUT


def test_the_engine_ranks_decisions_by_words_and_record_vectors_when_a_worker_runs(repo, fake_worker, monkeypatch):
    from aleph_zero import decisions
    from aleph_zero.embed import worker
    from aleph_zero.embed.client import EmbedClient

    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    engine = project(repo)
    task = "publish the order events on the bus"
    answer = engine.decisions(task)
    assert answer.vectors == "used" and len(answer.decisions) == 4                         # every record has a place, by words or by nearness
    assert all("vectors" in hit.sources for hit in answer.decisions)
    # the same order as the fusion worked out by hand from the same texts and the same worker
    client = EmbedClient(fake_worker, capacity=worker.capacity_tokens())
    kept = decisions.Records([hit for hit in engine._records[repo.root][1].decisions])
    vectors = client.embed([decisions.record_text(each) for each in kept.decisions], kind="document")
    query = client.embed([task[:decisions.CUT]], kind="query")[0]
    near = {each.path: float(vectors[at] @ query) for at, each in enumerate(kept.decisions)}
    assert [hit.number for hit in answer.decisions] == [hit.number for hit in kept.rank(task, nearness=near, a=decisions.A)]
    alone = engine.decisions(task, rrf_k=0)
    assert alone.vectors == "" and all(hit.sources == ("words",) for hit in alone.decisions)   # asked for words alone: words alone
    assert _kept(engine) == 4                                                           # the records' vectors are kept, by their text
    repo.write("docs/adr/0012-use-an-event-bus.md", BUS + "\nAmended.\n")
    assert engine.decisions(task).vectors == "used" and _kept(engine) == 5              # one record changed: one vector more


def test_without_a_worker_or_switched_off_by_hand_the_fused_ranker_is_words_alone_and_says_so(repo, fake_worker, monkeypatch):
    from pathlib import Path

    from aleph_zero import decisions
    from aleph_zero.embed import worker

    from aleph_zero.embed import setup

    monkeypatch.setattr(worker, "socket_path", lambda: Path("/tmp/az-no-such-worker.sock"))
    monkeypatch.setattr(setup, "installed", lambda: True)
    project(repo)
    answer = Engine(repo.root).decisions("publish the order events on the bus")
    assert all(hit.sources == ("words",) for hit in answer.decisions) and "not running" in answer.vectors
    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    off = Engine(repo.root, dense=False).decisions("publish the order events on the bus")
    assert all(hit.sources == ("words",) for hit in off.decisions) and off.vectors == ""


def _with_worker(repo, fake_worker, monkeypatch):
    from aleph_zero.embed import worker

    monkeypatch.setattr(worker, "socket_path", lambda: fake_worker)
    return project(repo)


def _kept(engine) -> int:
    return engine._index(engine._view(None)).conn.execute("SELECT count(*) FROM record_vectors").fetchone()[0]


def test_another_session_writing_the_index_does_not_break_or_hold_up_a_question(repo, fake_worker, monkeypatch):
    import time

    import apsw

    from aleph_zero.store import index_path

    engine = _with_worker(repo, fake_worker, monkeypatch)
    task = "publish the order events on the bus"
    assert engine.decisions(task).vectors == "used" and _kept(engine) == 4
    other = apsw.Connection(str(index_path(repo.root)))
    other.execute("BEGIN IMMEDIATE")                 # as `aleph embed run`, or another session, in the middle of a write
    try:
        repo.write("docs/adr/0012-use-an-event-bus.md", BUS + "\nAmended.\n")
        started = time.monotonic()
        answer = engine.decisions(task)              # the new vector is in hand: it is used, and only not kept this time
        assert answer.vectors == "used" and len(answer.decisions) == 4 and time.monotonic() - started < 3.0
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert _kept(engine) == 4 and engine.decisions(task).vectors == "used" and _kept(engine) == 5      # kept the next time


def test_record_vectors_are_made_in_parts_that_are_kept_and_a_worker_that_fails_is_not_asked_at_every_question(repo, fake_worker, monkeypatch):
    from aleph_zero import engine as engine_module
    from aleph_zero.embed.client import EmbedPool, Unavailable

    engine = _with_worker(repo, fake_worker, monkeypatch)
    monkeypatch.setattr(engine_module, "RECORD_BATCH", 2)
    real, asked = EmbedPool.embed, []

    def fails_after_the_first_part(self, texts, kind):
        asked.append(len(texts))
        if len(asked) == 2:
            raise Unavailable("the worker went away")
        return real(self, texts, kind)

    monkeypatch.setattr(EmbedPool, "embed", fails_after_the_first_part)
    task = "publish the order events on the bus"
    first = engine.decisions(task)
    assert all(hit.sources == ("words",) for hit in first.decisions) and "went away" in first.vectors
    assert asked == [2, 2] and _kept(engine) == 2                                  # the first part is kept
    again = engine.decisions(task)
    assert asked == [2, 2] and "went away" in again.vectors                        # not asked again at once: no stall at every question
    monkeypatch.setattr(engine_module, "RECORD_RETRY_SECONDS", 0.0)
    assert engine.decisions(task).vectors == "used" and asked == [2, 2, 2] and _kept(engine) == 4      # only what was missing


def test_the_first_question_does_not_wait_for_vectors_longer_than_it_is_allowed(repo, fake_worker, monkeypatch):
    from aleph_zero import engine as engine_module

    engine = _with_worker(repo, fake_worker, monkeypatch)
    monkeypatch.setattr(engine_module, "RECORD_EMBED_SECONDS", 0.0)
    answer = engine.decisions("publish the order events on the bus")
    assert all(hit.sources == ("words",) for hit in answer.decisions) and "still to be made" in answer.vectors and _kept(engine) == 0


def test_a_second_session_does_not_make_the_records_vectors_again(repo, fake_worker, monkeypatch):
    from aleph_zero.embed.client import EmbedClient

    def made() -> int:
        client = EmbedClient(fake_worker)
        try:
            return int(client.metrics().split("llamacpp:n_decode_total ")[1].split()[0])
        finally:
            client.close()

    engine = _with_worker(repo, fake_worker, monkeypatch)
    task = "publish the order events on the bus"
    assert engine.decisions(task).vectors == "used"
    before = made()
    engine.close()
    assert Engine(repo.root).decisions(task).vectors == "used" and made() == before + 1      # the task's own vector, nothing else


def test_asking_for_fusion_by_rank_gets_fusion_by_rank(repo, fake_worker, monkeypatch):
    """`rrf_k` given is the earlier fusion asked for by name (the benchmark's arms), whatever the default is."""
    engine = _with_worker(repo, fake_worker, monkeypatch)
    monkeypatch.setattr(Engine, "_record_nearness", lambda *_: (_ for _ in ()).throw(AssertionError("the record vectors were asked for")))
    engine.decisions("publish the order events on the bus", rrf_k=5)
    engine.decisions("publish the order events on the bus", rrf_k=0)


def test_someone_who_never_installed_the_worker_is_not_told_about_it_at_every_question(repo, monkeypatch):
    from pathlib import Path

    from aleph_zero.embed import setup, worker

    monkeypatch.setattr(worker, "socket_path", lambda: Path("/tmp/az-no-such-worker.sock"))
    project(repo)
    monkeypatch.setattr(setup, "installed", lambda: False)
    assert Engine(repo.root).decisions("publish the order events on the bus").vectors == ""
    monkeypatch.setattr(setup, "installed", lambda: True)
    assert "not running" in Engine(repo.root).decisions("publish the order events on the bus").vectors


def test_with_two_records_a_hair_s_difference_in_nearness_weighs_as_much_as_every_word():
    """Known, and left as it was measured: scaling over the records makes the nearer of two worth a full 1, however
    little nearer. On repositories with 6 and 12 records the fused ranking was level with words at 3 (ADR-003)."""
    from aleph_zero import decisions

    two = decisions.Records(_three()[:2])
    near = {"docs/adr/0001-x.md": 0.51, "docs/adr/0002-x.md": 0.50}
    assert [(hit.number, hit.score) for hit in two.rank("queue per kind of work", nearness=near, a=0.5)] == [(1, 0.5), (2, 0.5)]
    assert [hit.number for hit in two.rank("queue per kind of work")] == [2]
