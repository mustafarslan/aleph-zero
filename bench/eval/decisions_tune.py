"""Choose the decision ranker's settings on development pairs, repository by repository left out.

Usage:
  uv run python bench/eval/decisions_tune.py bench/data/decisions-pub --labels labels-pub.json [--out FILE]

`<workdir>` holds a clone of each corpus and the labels `decision_labels.py` wrote. Words only: no
vectors, no worker. Nothing here reads oxn's 100 pairs.

The protocol, written down on 2026-10-12 before any setting was measured on these pairs:

- pairs and candidates: as the labels file has them (the tightened citation rule; the records listed
  there). The task text is the harness's (`decisions.task_text`): the commit minus the citation and
  the plumbing.
- the settings tried: a word of the title counts 1, 2 or 3 times; of the Decision section 1 or 2
  times; a word of the task's first line 1 or 2 times; BM25's b 0.75 or 0.4; words as written or
  reduced to stems. 48 settings. The first (1, 1, 1, 0.75, as written) is the ranker as measured.
- choosing: a corpus with at least ten pairs is a fold. For each fold, the setting with the highest
  precision at 3 on the pairs of all other corpora (ties: higher MRR, then the earlier setting in the
  list, which is the one nearer the measured ranker) is scored on the fold. The pooled result over the
  folds is the estimate of what choosing this way gives on a repository not seen.
- beside it, on the same pairs: the measured ranker, and oxn's own ranker (`decisions.reference_order`).
- the difference in precision at 3 to each is given with a 90% interval from 10,000 paired resamples
  of the pairs (seed 20261012).
- what is taken further: the setting chosen on all pairs together, if (a) the pooled held-out precision
  at 3 is at least 2 points above the measured ranker's with MRR not lower, and (b) at least two folds
  choose a setting that agrees with it in the title weight and in b. Otherwise the measured ranker
  stands and there is nothing new to run on the 100.
- the one run on oxn's 100 pairs with a setting taken further is the third on those pairs and is
  reported whatever it shows. It becomes the engine's default only if its precision at 3 there is not
  below the 0.820 measured now. "Better than oxn" is said only if it is above 0.820 there and not below
  oxn's ranker on the held-out pairs here.

Vectors (`--dense`; written down on 2026-10-12 after the word settings were measured and none was taken
further, and before any vector was made for these pairs):

- the words are the measured ranker's: its BM25 score for each record.
- a record's nearness to a task is the cosine of the task's vector (the task text, cut to 5,000
  characters, embedded as a query) with the record's (embedded as a document), in two variants:
  `record`, one vector of the title, the Decision section and then the body from its start, cut to
  5,000 characters; `passages`, the body cut at blank lines into passages of at most 1,000 characters,
  each under the title, the record's nearness being that of its nearest passage.
- fusion: for one task both scores are scaled to 0..1 over the candidates (from the lowest to the
  highest; a record that shares no word with the task has a BM25 of 0) and added as
  a * words + (1 - a) * nearness, with a one of 0.9, 0.8, 0.7, 0.6, 0.5. Ties: the record's number.
- ten settings beside the measured ranker. Choosing as above. Taken further only if the pooled
  held-out precision at 3 is at least 2 points above the measured ranker's with MRR not lower, and at
  least two folds choose the variant of the overall choice with an `a` within 0.1 of its.
- if taken further: it is built into the engine under tests, one of them that the engine's order
  equals this script's on every development pair, and then run once on oxn's 100 through the engine.
  That is the third run of `aleph decisions` on those pairs (after words, and stems) and the fifth of
  anything of Aleph's (ordinary search by words, and with vectors).
- if not: this effort ends here. By words the two rankers are one function, and no reranking model
  goes into the engine.

Second round (`--dense --weights low`; written down on 2026-10-13, after eleven more public repositories
were mined with the frozen rule and gave 38 pairs, and before any ranker was scored on them):

- first, untouched: the ranker as it ships (`record`, a = 0.5) and the word ranker are scored on the 38
  new pairs with `decisions.py`. That is a fifth set the ranker was never chosen on, and is reported
  whatever it shows.
- then a tuning round on all development pairs (the 89 and the 38), each corpus with ten pairs or more
  left out in turn, the others always among those chosen on. The grid is the `record` variant with
  a from 0.9 down to 0.1 in steps of 0.1: the first round's best was at the edge of its grid.
- what replaces a = 0.5: the setting chosen on all pairs, only if on the pooled held-out folds its
  precision at 3 is not below that of a = 0.5, its precision at 1 is not below the word ranker's and is
  above that of a = 0.5, its MRR is above that of a = 0.5, and at least two folds choose an a within 0.1
  of it. The condition on precision at 1 is new: on oxn's 100 pairs the shipped ranker is three pairs
  below words at 1, and an adversarial reader would not have shipped it without that holding.
- a setting that replaces it is run once on oxn's 100 pairs through the engine: the fourth run of
  `aleph decisions` there. It becomes the default only if there its precision at 3 is not below 0.840
  and its precision at 1 not below 0.580, the shipped ranker's figures. Otherwise a = 0.5 stays.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decisions as harness  # noqa: E402

from aleph_zero import decisions as records_of  # noqa: E402

SEED = 20261012
SETTINGS = [{"title": title, "decision": decision, "subject": subject, "b": b, "stems": stems}
            for stems, b, subject, decision, title in itertools.product((False, True), (0.75, 0.4), (1.0, 2.0), (1.0, 2.0), (1.0, 2.0, 3.0))]
MEASURED = SETTINGS[0]
FOLD = 10


def load(workdir: Path, labels: dict) -> dict[str, dict]:
    """Per corpus: the records as the engine parses them, their raw texts, and each pair's task text and gold."""
    corpora = {}
    for name, spec in labels["corpora"].items():
        root = workdir / name
        texts = {path: harness.git(root, "show", f"{spec['sha']}:{path}") for path in spec["records"]}
        parsed = [records_of.parse(path, records_of.is_record(path), text) for path, text in texts.items()]
        corpora[name] = {"parsed": parsed, "texts": texts, "numbers": {each.path: each.number for each in parsed},
                         "pairs": [{"sha": pair["sha"], "gold": set(pair["gold"]), "task": harness.task_text(root, pair["sha"])}
                                   for pair in spec["pairs"]]}
    return corpora


def orders(corpus: dict, setting: dict) -> list[list[int]]:
    ranker = records_of.Records(corpus["parsed"], stems=setting["stems"], b=setting["b"],
                                fields=(setting["title"], setting["decision"], 1.0), subject=setting["subject"])
    return [[ranker.decisions[at].number for at, _ in ranker.by_words(pair["task"])] for pair in corpus["pairs"]]


def rows_of(corpus: dict, ranked: list[list[int]]) -> list[dict]:
    return [{"gold": pair["gold"], "order": order} for pair, order in zip(corpus["pairs"], ranked)]


def hit3(row: dict) -> int:
    return int(any(number in row["gold"] for number in row["order"][:3]))


def interval(ours: list[dict], theirs: list[dict]) -> list[float]:
    """The difference in precision at 3, ours minus theirs, with a 90% interval over paired resamples."""
    gains = [hit3(a) - hit3(b) for a, b in zip(ours, theirs)]
    chooser = random.Random(SEED)
    means = sorted(sum(chooser.choices(gains, k=len(gains))) / len(gains) for _ in range(10_000))
    return [round(sum(gains) / len(gains), 3), round(means[500], 3), round(means[9500], 3)]


def best(table: dict[int, dict[str, list[dict]]], names: list[str]) -> int:
    """The setting with the highest precision at 3 on the named corpora; ties: MRR, then the earlier setting."""
    def key(at: int):
        found = harness.score([row for name in names for row in table[at][name]])
        return (-found["p_at_3"], -found["mrr"], at)
    return min(range(len(SETTINGS)), key=key)


# ---- vectors -------------------------------------------------------------------------------------------

CUT, PASSAGE = records_of.CUT, 1000
VARIANTS = ("record", "passages")
WEIGHTS = (0.9, 0.8, 0.7, 0.6, 0.5)
DENSE = [{"variant": variant, "a": a} for variant in VARIANTS for a in WEIGHTS]
record_text = records_of.record_text       # one definition: what the engine embeds for a record is what was measured


def passages(each) -> list[str]:
    out, held = [], ""
    for block in each.text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if held and len(held) + len(block) + 2 > PASSAGE:
            out.append(held)
            held = ""
        held = f"{held}\n\n{block}" if held else block
        while len(held) > PASSAGE:
            out.append(held[:PASSAGE])
            held = held[PASSAGE:]
    if held:
        out.append(held)
    return [f"{each.title}\n\n{passage}" for passage in out] or [each.title]


class Vectors:
    """Vectors of texts, made once by the embedding worker and kept beside the clones."""

    def __init__(self, workdir: Path) -> None:
        import numpy as np
        from aleph_zero.embed import client

        self._np, self._kind, self._file = np, client.VECTOR_KIND, workdir / "tune-vectors.npz"
        self._known = dict(np.load(self._file)) if self._file.exists() else {}

    def of(self, texts: list[str], kind: str):
        import hashlib

        from aleph_zero.embed import client, worker

        keys = [hashlib.sha256(f"{self._kind}\0{kind}\0{text}".encode()).hexdigest() for text in texts]
        missing = [at for at, key in enumerate(keys) if key not in self._known]
        if missing:
            pool = client.EmbedPool(worker.ensure(), capacity=worker.capacity_tokens(), connections=worker.slots())
            try:
                made = pool.embed([texts[at] for at in missing], kind)
            finally:
                pool.close()
            for at, row in zip(missing, made):
                self._known[keys[at]] = row
            self._np.savez(self._file, **self._known)
        return self._np.stack([self._known[key] for key in keys]) if keys else self._np.zeros((0, 1))


def nearness(corpus: dict, vectors: Vectors) -> dict[str, list[list[float]]]:
    """Per variant: for each pair, the cosine of its task with every record, in the records' order."""
    np = vectors._np
    tasks = vectors.of([pair["task"][:CUT] or "(empty)" for pair in corpus["pairs"]], "query")
    whole = vectors.of([record_text(each) for each in corpus["parsed"]], "document")
    cut = [passages(each) for each in corpus["parsed"]]
    flat = vectors.of([passage for some in cut for passage in some], "document")
    owner = np.array([at for at, some in enumerate(cut) for _ in some])
    by_passage = tasks @ flat.T
    nearest = np.stack([by_passage[:, owner == at].max(axis=1) for at in range(len(cut))], axis=1)
    return {"record": (tasks @ whole.T).tolist(), "passages": nearest.tolist()}


def scaled(values: list[float]) -> list[float]:
    low, high = min(values), max(values)
    return [(value - low) / (high - low) if high > low else 0.0 for value in values]


def fused(corpus: dict, near: list[list[float]], a: float) -> list[tuple[list[int], list[str], dict[str, float]]]:
    """Per pair: the records' numbers in fused order, their paths in that order, and each path's fused score."""
    ranker = records_of.Records(corpus["parsed"], stems=False, b=0.75, fields=(1.0, 1.0, 1.0), subject=1.0)
    numbers, paths = [each.number for each in ranker.decisions], [each.path for each in ranker.decisions]
    place = {each.path: at for at, each in enumerate(corpus["parsed"])}        # `near` is in the order of `parsed`
    out = []
    for pair, cosines in zip(corpus["pairs"], near):
        words = dict(ranker.by_words(pair["task"]))
        by_words = scaled([words.get(at, 0.0) for at in range(len(numbers))])
        by_nearness = scaled([cosines[place[path]] for path in paths])
        total = [a * w + (1 - a) * n for w, n in zip(by_words, by_nearness)]
        ordered = sorted(range(len(numbers)), key=lambda at: (-total[at], at))     # the records' own order: by number, then path
        out.append(([numbers[at] for at in ordered], [paths[at] for at in ordered], dict(zip(paths, total))))
    return out


def fused_orders(corpus: dict, near: list[list[float]], a: float) -> list[list[int]]:
    return [order for order, _, _ in fused(corpus, near, a)]


# Two records whose fused scores are nearer than this may stand either way round. The worker's vectors change in
# their last digits with how texts are batched (measured: up to 1.3e-4 in the cosine of a task with a record,
# batch against one at a time), and scaling to 0..1 stretches that. First set at 1e-5, before the noise was measured.
TIE = 1e-3


def check_engine(workdir: Path, labels: dict, corpora: dict, near: dict) -> dict:
    """Whether the engine, with the fusion switched on, ranks every development pair as this script does."""
    import tempfile

    from aleph_zero import engine as engine_module
    from aleph_zero.engine import Engine

    engine_module.QUERY_EMBED_SECONDS = 30.0        # a busy worker must not turn a check of the fusion into a check of words
    engine_module.RECORD_EMBED_SECONDS, engine_module.RECORD_RETRY_SECONDS = 300.0, 0.0     # nor the limit on a first question
    records_of.FUSION = "convex"
    same = ties = different = unused = first_three = 0
    examples = []
    with tempfile.TemporaryDirectory(prefix="aleph-decisions-") as scratch:
        for name, spec in labels["corpora"].items():
            where = harness.scratch_repository(workdir / name, spec["sha"], spec["records"], Path(scratch) / name)
            engine = Engine(where)
            for pair, (_, order, total) in zip(corpora[name]["pairs"], fused(corpora[name], near[name]["record"], records_of.A)):
                answer = engine.decisions(pair["task"], k=len(order))
                unused += answer.vectors != "used"
                theirs = [hit.path for hit in answer.decisions]
                first_three += theirs[:3] == order[:3]
                if theirs == order:
                    same += 1
                elif sorted(theirs) == sorted(order) and all(one == other or abs(total[one] - total[other]) < TIE for one, other in zip(theirs, order)):
                    ties += 1
                else:
                    different += 1
                    examples.append({"corpus": name, "sha": pair["sha"][:10], "engine": theirs[:5], "script": order[:5], "vectors": answer.vectors})
            engine.close()
    return {"pairs": same + ties + different, "the same order": same, "the same but for ties": ties, "different": different,
            "the same first three": first_three, "answered without vectors": unused, "examples": examples[:5]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--labels", default="labels-pub.json")
    parser.add_argument("--dense", action="store_true", help="the ten settings with vectors instead of the 48 by words (needs the embed worker)")
    parser.add_argument("--weights", choices=("first", "low"), default="first",
                        help="with --dense: the first round's ten settings, or the second round's: one vector a record, a from 0.9 to 0.1")
    parser.add_argument("--check-engine", action="store_true",
                        help="whether the engine, with the fusion switched on, gives this script's order on every pair (needs the embed worker)")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    labels = json.loads((args.workdir / args.labels).read_text())
    corpora = load(args.workdir, labels)
    global SETTINGS
    if args.check_engine:
        vectors = Vectors(args.workdir)
        found = check_engine(args.workdir, labels, corpora, {name: nearness(corpus, vectors) for name, corpus in corpora.items()})
        print(json.dumps(found, indent=2))
        return 0 if not found["different"] and not found["answered without vectors"] else 1
    if args.dense:
        vectors = Vectors(args.workdir)
        near = {name: nearness(corpus, vectors) for name, corpus in corpora.items()}
        dense = DENSE if args.weights == "first" else [{"variant": "record", "a": round(a / 10, 1)} for a in range(9, 0, -1)]
        SETTINGS = [MEASURED, *dense]
        table = {0: {name: rows_of(corpus, orders(corpus, MEASURED)) for name, corpus in corpora.items()}}
        for at, setting in enumerate(dense, 1):
            table[at] = {name: rows_of(corpus, fused_orders(corpus, near[name][setting["variant"]], setting["a"])) for name, corpus in corpora.items()}
    else:
        table = {at: {name: rows_of(corpus, orders(corpus, setting)) for name, corpus in corpora.items()} for at, setting in enumerate(SETTINGS)}
    reference = {name: rows_of(corpus, [harness.reference_order(corpus["texts"], corpus["numbers"], pair["task"]) for pair in corpus["pairs"]])
                 for name, corpus in corpora.items()}
    folds = [name for name, corpus in corpora.items() if len(corpus["pairs"]) >= FOLD]
    chosen, held, measured, theirs = {}, [], [], []
    for fold in folds:
        at = best(table, [name for name in corpora if name != fold])
        chosen[fold] = at
        held += table[at][fold]
        measured += table[0][fold]
        theirs += reference[fold]
    overall = best(table, list(corpora))
    everything = lambda rows: [row for name in corpora for row in rows[name]]  # noqa: E731
    gain = interval(held, measured) if held else [0.0, 0.0, 0.0]
    same = (lambda one, other: one.get("variant") == other.get("variant") and abs(one.get("a", 1.0) - other.get("a", 1.0)) <= 0.1001) if args.dense \
        else (lambda one, other: one["title"] == other["title"] and one["b"] == other["b"])
    agree = sum(same(SETTINGS[at], SETTINGS[overall]) for at in chosen.values())
    report = {
        "pairs": {name: len(corpus["pairs"]) for name, corpus in corpora.items()},
        "records": {name: len(corpus["parsed"]) for name, corpus in corpora.items()},
        "folds": {fold: {"chosen on the others": SETTINGS[at], "on this fold": harness.score(table[at][fold]),
                         "the measured ranker": harness.score(table[0][fold]), "oxn's ranker": harness.score(reference[fold])}
                  for fold, at in chosen.items()},
        "held out, pooled": {"chosen": harness.score(held), "the measured ranker": harness.score(measured), "oxn's ranker": harness.score(theirs),
                             "p_at_3 chosen minus measured [difference, 5%, 95%]": gain,
                             "p_at_3 chosen minus oxn's [difference, 5%, 95%]": interval(held, theirs) if held else None,
                             "p_at_3 measured minus oxn's [difference, 5%, 95%]": interval(measured, theirs) if held else None},
        "on all pairs (not held out)": {"chosen": SETTINGS[overall], "its score": harness.score(everything(table[overall])),
                                        "the measured ranker": harness.score(everything(table[0])), "oxn's ranker": harness.score(everything(reference))},
        "the shipped setting (record, a = 0.5) on the same folds": (lambda at: harness.score([row for fold in folds for row in table[at][fold]]) if at is not None else None)(
            next((at for at, setting in enumerate(SETTINGS) if setting.get("variant") == "record" and setting.get("a") == 0.5), None)),
        "every setting on the same folds (each fixed, not chosen)": [{**SETTINGS[at], **harness.score([row for fold in folds for row in table[at][fold]])} for at in sorted(table)],
        "taken further": bool(held) and overall != 0 and gain[0] >= 0.02 and harness.score(held)["mrr"] >= harness.score(measured)["mrr"] and agree >= 2,
        "folds that agree with the overall choice": agree,
        "every setting on all pairs (not held out)": [{**SETTINGS[at], **harness.score(everything(table[at]))} for at in sorted(table)],
    }
    print(json.dumps(report, indent=2))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
