"""The vector's width and the document's title, read on localisation: the arithmetic of the experiment's script."""
import subprocess
import sys
from pathlib import Path

import numpy as np

EVAL = Path(__file__).resolve().parents[1] / "bench" / "eval"
sys.path.insert(0, str(EVAL))
import widths  # noqa: E402


def test_a_vector_read_at_a_smaller_width_is_cut_and_brought_back_to_length_one_as_the_engine_cuts_it():
    rng = np.random.default_rng(7)
    full = rng.normal(size=(5, 768)).astype(np.float32)
    full /= np.linalg.norm(full, axis=1, keepdims=True)
    cut = widths.cut(full.astype(np.float16), 256)
    assert cut.shape == (5, 256) and np.allclose(np.linalg.norm(cut, axis=1), 1.0, atol=1e-3)
    expected = full[:, :256] / np.linalg.norm(full[:, :256], axis=1, keepdims=True)      # what `EmbedClient.embed` keeps
    assert np.allclose(cut, expected, atol=2e-3)
    query = widths.cut(full[0], 256)
    assert int(np.argmax(cut @ query)) == 0


def test_scores_at_a_width_are_those_of_vectors_kept_at_that_width():
    rng = np.random.default_rng(11)
    stored = rng.normal(size=(40, 768)).astype(np.float32)
    stored = (stored / np.linalg.norm(stored, axis=1, keepdims=True)).astype(np.float16)
    query = stored[3].astype(np.float32)
    plain = lambda mapped, rows, vector: np.asarray([mapped[row] for row in rows], dtype=np.float32) @ vector  # noqa: E731
    scores = widths.cut_scores(plain, 512)(stored, list(range(40)), query)
    kept = widths.cut(stored, 512)
    assert np.allclose(scores, kept @ widths.cut(query, 512), atol=1e-4) and int(np.argmax(scores)) == 3


def test_each_title_has_a_kind_of_its_own_and_the_text_that_goes_with_it():
    assert len({widths.kind_of(title) for title in widths.TITLES}) == 3 and all(widths.kind_of(title).endswith("/768") for title in widths.TITLES)
    text = lambda breadcrumb, signature, body: "\n".join(part for part in (breadcrumb, signature, body) if part)  # noqa: E731
    crumb = widths.document("breadcrumb", "TokenStore > rotate", "def rotate(self):", "    return 1", "src/a.py", text)
    assert crumb == "title: TokenStore > rotate | text: def rotate(self):\n    return 1"         # the breadcrumb is said once
    assert widths.document("breadcrumb", "", "", "x = 1", None, text) == "title: none | text: x = 1"
    path = widths.document("path", "TokenStore > rotate", "def rotate(self):", "    return 1", "src/a.py", text)
    assert path == "title: src/a.py | text: TokenStore > rotate\ndef rotate(self):\n    return 1"


def test_the_engine_s_modules_are_pointed_at_the_experiment_s_vectors_in_a_process_of_their_own():
    code = ("import sys; sys.path.insert(0, %r); import widths; widths.setup('breadcrumb', 512); "
            "from aleph_zero import vectors, vectorfile; from aleph_zero.embed import client; import aleph_zero.embed.client as fresh; "
            "print(client.WIDTH, vectorfile.ROW_BYTES, vectors.WIDTH, client.VECTOR_KIND, repr(client.DOCUMENT_PREFIX))" % str(EVAL))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.stdout.split() == ["768", "1536", "768", "embeddinggemma-2-q8/crumb1/768", "''"], out.stderr[-400:]
    from aleph_zero.embed import client           # and this process, which did not ask, is as it was

    assert client.WIDTH == 256 and client.VECTOR_KIND.endswith("/p1/256")


def test_two_results_are_compared_on_the_questions_both_scored_by_main_language():
    def row(name, repo, hit):
        score = {"recall@10": hit, "recall@1": hit}
        return {"instance": name, "repo": repo, "scores": {"title": {"hybrid": score}, "full": {"hybrid": score}}}

    base = [row(f"js-{n}", "o/js", 0.0) for n in range(10)] + [row(f"py-{n}", "o/py", 1.0) for n in range(10)] + [row("only-here", "o/py", 1.0)]
    other = [row(f"js-{n}", "o/js", 1.0) for n in range(10)] + [row(f"py-{n}", "o/py", 1.0) for n in range(10)]
    languages = {**{f"js-{n}": "javascript" for n in range(10)}, **{f"py-{n}": "python" for n in range(10)}}
    report = widths.compare(base, other, languages, lambda language: language == "javascript")
    assert report["javascript"]["first line, recall@10"]["gain"] == 1.0 and report["python"]["first line, recall@10"]["gain"] == 0.0
    assert report["all languages"]["questions"] == 20 and report["all languages"]["first line, recall@10"]["gain"] == 0.5
    assert report["languages that ship with vectors"]["questions"] == 10
