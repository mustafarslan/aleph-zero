import multiprocessing
from pathlib import Path

from aleph_zero import embedder
from aleph_zero.embed.client import VECTOR_KIND, EmbedClient
from aleph_zero.indexer import Index
from aleph_zero.store import index_path, open_db
from aleph_zero.vectors import VectorStore
from aleph_zero.view import build_view


def ready(repo):
    index = Index(open_db(index_path(repo.root)))
    index.sync(build_view(repo.root))
    return index, VectorStore(index.conn, index_path(repo.root).parent, VECTOR_KIND)


def served(sock):
    client = EmbedClient(sock)
    try:
        return int(next(line.split()[1] for line in client.metrics().splitlines() if line.startswith("llamacpp:n_decode_total")))
    finally:
        client.close()


def test_every_chunk_of_the_view_gets_a_vector(repo, fake_worker):
    index, store = ready(repo)
    total = store.coverage(index)[1]
    report = embedder.embed_pending(index, store, EmbedClient(fake_worker))
    assert (report.embedded, report.remaining, report.reason) == (total, 0, "")
    assert store.missing(index) == [] and served(fake_worker) == total


def test_work_stops_at_its_budget_and_resumes_without_repeating(repo, fake_worker):
    index, store = ready(repo)
    total = store.coverage(index)[1]
    first = embedder.embed_pending(index, store, EmbedClient(fake_worker), budget_seconds=0.0, batch=2)
    assert first.embedded == 2 and first.remaining == total - 2   # one batch is always attempted
    stopped = embedder.embed_pending(index, store, EmbedClient(fake_worker), stop=lambda: True)
    assert stopped.embedded == 0 and stopped.remaining == total - 2
    rest = embedder.embed_pending(index, store, EmbedClient(fake_worker), batch=2)
    assert rest.embedded == total - 2 and rest.remaining == 0 and served(fake_worker) == total


def test_what_is_embedded_is_where_the_chunk_sits_its_signature_and_its_text(repo, fake_worker):
    index, store = ready(repo)
    sent = []

    class Spy(EmbedClient):
        def embed(self, texts, kind):
            sent.extend((kind, text) for text in texts)
            return super().embed(texts, kind)

    embedder.embed_pending(index, store, Spy(fake_worker))
    refresh = next(text for kind, text in sent if "def refresh_token" in text)
    assert refresh.startswith("refresh_token\ndef refresh_token(session)\ndef refresh_token(session):")
    assert all(kind == "document" for kind, _ in sent)
    assert not any("src/auth" in text for _, text in sent)   # a chunk is shared by every file that holds it: no path
    section = next(text for _, text in sent if "Tokens expire" in text)
    assert section.startswith("Guide > Authentication\n## Authentication")   # a heading is not repeated as a signature


def test_a_missing_worker_is_reported_not_raised(repo, tmp_path):
    index, store = ready(repo)
    report = embedder.embed_pending(index, store, EmbedClient(tmp_path / "nobody.sock"))
    assert report.embedded == 0 and report.remaining > 0 and "cannot be reached" in report.reason


def _hold_lock(root, started, release):
    from aleph_zero import embedder as child_embedder

    with child_embedder.embedding_lock(Path(root)) as held:
        started.put(held)
        release.get(timeout=30)


def test_only_one_session_embeds_a_repository_at_a_time(repo, fake_worker):
    index, store = ready(repo)
    context = multiprocessing.get_context("spawn")
    started, release = context.Queue(), context.Queue()
    other = context.Process(target=_hold_lock, args=(str(index_path(repo.root).parent), started, release))
    other.start()
    assert started.get(timeout=30) is True
    report = embedder.embed_pending(index, store, EmbedClient(fake_worker))
    assert report.embedded == 0 and "another session" in report.reason and served(fake_worker) == 0
    release.put(True)
    other.join()
    assert embedder.embed_pending(index, store, EmbedClient(fake_worker)).remaining == 0


def test_a_chunk_collected_while_it_was_being_embedded_is_dropped_quietly(repo, fake_worker):
    index, store = ready(repo)

    class Collecting(EmbedClient):
        def embed(self, texts, kind):
            rows = super().embed(texts, kind)
            index.conn.execute("DELETE FROM blobs")          # another session's garbage collection, mid-batch
            index.conn.execute("DELETE FROM chunks")
            return rows

    report = embedder.embed_pending(index, store, Collecting(fake_worker))
    assert report.embedded == 0
    assert index.conn.execute("SELECT count(*) FROM chunk_vectors").fetchall() == [(0,)]
