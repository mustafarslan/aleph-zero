"""Which split threshold retrieves best? Docstring -> function on real Python repositories.

Query: the first sentence of a function's docstring. Corpus: every function with the
docstring removed (so the query cannot match itself), embedded whole when it fits the
threshold and otherwise split at statement boundaries into pieces under it. A function
is found when any of its pieces is retrieved. Results are reported by function length,
because the threshold only matters for long functions.
"""
import argparse
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tree_sitter_language_pack as pack

import embed_backends

DATA = Path(__file__).resolve().parents[1] / "data"
THRESHOLDS = (256, 512, 1024, 2048, 4096)
BUCKETS = ((0, 256), (256, 512), (512, 1024), (1024, 2048), (2048, 10**9))
DIM = 256
MIN_QUERY_WORDS = 6
_PARSER = None


@dataclass
class Function:
    path: str
    breadcrumb: str
    signature: str
    statements: list[str]
    query: str | None
    tokens: int = 0


def _parser():
    global _PARSER
    if _PARSER is None:
        _PARSER = pack.get_parser("python")
    return _PARSER


def _first_sentence(docstring: str) -> str | None:
    text = docstring.strip().strip("\"'").strip()
    text = re.sub(r"^[rRbBuU]*(\"\"\"|'''|\"|')", "", text)
    first = re.split(r"\n\s*\n", text, maxsplit=1)[0]
    first = " ".join(first.split())
    match = re.match(r"(.+?[.!?])(\s|$)", first)
    sentence = match.group(1) if match else first
    return sentence if len(sentence.split()) >= MIN_QUERY_WORDS else None


def extract_functions(source: bytes, path: str) -> list[Function]:
    found: list[Function] = []

    def text(node) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", "replace")

    def visit(node, scope: tuple[str, ...]) -> None:
        for child in node.named_children:
            inner = child
            if child.type == "decorated_definition":
                inner = child.child_by_field_name("definition") or child
            if inner.type not in ("function_definition", "class_definition"):
                continue
            name_node, body = inner.child_by_field_name("name"), inner.child_by_field_name("body")
            if name_node is None or body is None:
                continue
            name = text(name_node)
            if inner.type == "class_definition":
                visit(body, scope + (name,))
                continue
            statements = list(body.named_children)
            query = None
            # This grammar build yields the docstring as a bare `string` statement; older
            # ones wrap it in an expression_statement. Accept both.
            first = statements[0] if statements else None
            if first is not None and first.type == "expression_statement" and first.named_child_count == 1:
                first = first.named_children[0] if first.named_children[0].type == "string" else None
            if first is not None and first.type == "string":
                query = _first_sentence(text(first))
                statements = statements[1:]
            statements = [s for s in statements if s.type != "comment"]
            if not statements:
                continue
            signature = source[child.start_byte:body.start_byte].decode("utf-8", "replace").rstrip()
            found.append(Function(path, " > ".join(scope + (name,)), signature, [text(s) for s in statements], query))

    visit(_parser().parse(source).root_node, ())
    return found


def split_sizes(sizes: list[int], overhead: int, cap: int) -> list[list[int]]:
    """Greedy statement packing: each piece's size plus the header overhead stays within the cap."""
    pieces: list[list[int]] = []
    current: list[int] = []
    used = overhead
    for index, size in enumerate(sizes):
        if current and used + size > cap:
            pieces.append(current)
            current, used = [], overhead
        current.append(index)
        used += size
    if current:
        pieces.append(current)
    return pieces


def function_ranks(scores: np.ndarray, func_of_chunk: np.ndarray, targets: list[int]) -> np.ndarray:
    n_funcs = int(func_of_chunk.max()) + 1
    ranks = np.empty(len(targets), dtype=np.int64)
    for row, target in enumerate(targets):
        best = np.full(n_funcs, -np.inf, dtype=np.float32)
        np.maximum.at(best, func_of_chunk, scores[row])
        ranks[row] = int((best > best[target]).sum()) + 1
    return ranks


def chunk_texts(function: Function, cap: int, per_char: float) -> list[str]:
    header = f"# {function.path} > {function.breadcrumb}\n"
    whole = header + function.signature + "\n" + "\n".join("    " + s for s in function.statements)
    if function.tokens <= cap:
        return [whole]
    overhead = int((len(header) + len(function.signature)) * per_char) + 8
    sizes = [int(len(s) * per_char) + 1 for s in function.statements]
    texts = []
    for piece in split_sizes(sizes, overhead, cap):
        texts.append(header + function.signature + "\n" + "\n".join("    " + function.statements[i] for i in piece))
    return texts


class Cache:
    """Embeddings by text hash, so a chunk shared by several thresholds is embedded once."""

    def __init__(self, embedder, tag: str) -> None:
        self._embedder, self._tag = embedder, tag
        self._path = DATA / "cache" / f"cap_quality_{tag}.npz"
        self._store: dict[str, np.ndarray] = {}
        if self._path.exists():
            loaded = np.load(self._path)
            self._store = {key: loaded[key] for key in loaded.files}
        self.embedded = 0

    def get(self, texts: list[str], prefix: str) -> np.ndarray:
        rows = []
        for text in texts:
            key = hashlib.sha1((prefix + text).encode()).hexdigest()
            if key not in self._store:
                self._store[key] = self._embedder.embed([prefix + text])[0]
                self.embedded += 1
            rows.append(self._store[key])
        return np.stack(rows)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(self._path, **self._store)


def truncate(rows: np.ndarray) -> np.ndarray:
    cut = rows[:, :DIM].astype(np.float32)
    return cut / np.maximum(np.linalg.norm(cut, axis=1, keepdims=True), 1e-12)


def summarize(ranks: np.ndarray) -> dict:
    return {
        "n": int(len(ranks)),
        "recall@1": round(float((ranks <= 1).mean()), 4),
        "recall@5": round(float((ranks <= 5).mean()), 4),
        "recall@10": round(float((ranks <= 10).mean()), 4),
        "mrr@10": round(float(np.where(ranks <= 10, 1.0 / ranks, 0.0).mean()), 4),
    }


def _per_query(ranks: np.ndarray, metric: str) -> np.ndarray:
    if metric == "recall@10":
        return (ranks <= 10).astype(np.float64)
    if metric == "mrr@10":
        return np.where(ranks <= 10, 1.0 / ranks, 0.0)
    raise ValueError(metric)


def paired_bootstrap(ranks_a: np.ndarray, ranks_b: np.ndarray, metric: str, draws: int = 2000, seed: int = 0):
    """Mean of (A - B) on the same queries, with a 95% interval from resampling queries."""
    diff = _per_query(np.asarray(ranks_a), metric) - _per_query(np.asarray(ranks_b), metric)
    rng = np.random.default_rng(seed)
    samples = diff[rng.integers(0, len(diff), size=(draws, len(diff)))].mean(axis=1)
    low, high = np.percentile(samples, [2.5, 97.5])
    return round(float(diff.mean()), 4), round(float(low), 4), round(float(high), 4)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repos", nargs="+", type=Path)
    parser.add_argument("--config", default="gguf-q8-gpu")
    parser.add_argument("--max-queries", type=int, default=1500)
    parser.add_argument("--thresholds", nargs="+", type=int, default=list(THRESHOLDS))
    parser.add_argument("--baseline", type=int, default=4096, help="threshold the others are compared against")
    parser.add_argument("--skip-truncate", action="store_true")
    args = parser.parse_args()
    all_ranks: dict[str, np.ndarray] = {}

    functions: list[Function] = []
    for repo in args.repos:
        for path in sorted(repo.rglob("*.py")):
            source = path.read_bytes()
            if len(source) <= 300_000:
                functions += extract_functions(source, str(path.relative_to(repo.parent)))
    embedder = embed_backends.load(args.config, n_ctx=4096)
    try:
        for function in functions:
            function.tokens = embedder.token_count(
                embed_backends.DOC_PREFIX + function.signature + "\n" + "\n".join(function.statements))
        chars = sum(len(f.signature) + sum(len(s) for s in f.statements) for f in functions)
        per_char = sum(f.tokens for f in functions) / chars
        rng = np.random.default_rng(0)
        candidates = [i for i, f in enumerate(functions) if f.query]
        long_ones = [i for i in candidates if functions[i].tokens > 512]
        short_ones = [i for i in candidates if functions[i].tokens <= 512]
        keep = long_ones + [short_ones[j] for j in rng.permutation(len(short_ones))[: max(0, args.max_queries - len(long_ones))]]
        targets = sorted(keep)
        cache = Cache(embedder, args.config)
        queries = truncate(cache.get([functions[i].query for i in targets], embed_backends.QUERY_PREFIX))
        lengths = np.array([functions[i].tokens for i in targets])
        report: dict = {
            "functions": len(functions), "queries": len(targets), "tokens_per_char": round(per_char, 4),
            "queries_by_length": {f"{lo}-{hi if hi < 10**9 else 'up'}": int(((lengths > lo) & (lengths <= hi)).sum()) for lo, hi in BUCKETS},
            "thresholds": {},
        }

        def evaluate(label: str, chunks_of: list[list[str]], cache_: Cache) -> None:
            started, before = time.perf_counter(), cache_.embedded
            func_of_chunk = np.array([i for i, texts in enumerate(chunks_of) for _ in texts])
            docs = truncate(cache_.get([t for texts in chunks_of for t in texts], embed_backends.DOC_PREFIX))
            ranks = function_ranks(queries @ docs.T, func_of_chunk, targets)
            all_ranks[label] = ranks
            entry = {"chunks": int(len(func_of_chunk)), "newly_embedded": cache_.embedded - before,
                     "seconds": round(time.perf_counter() - started, 1), "all": summarize(ranks)}
            for lo, hi in BUCKETS:
                mask = (lengths > lo) & (lengths <= hi)
                if mask.any():
                    entry[f"{lo}-{hi if hi < 10**9 else 'up'}"] = summarize(ranks[mask])
            report["thresholds"][label] = entry
            print(label, json.dumps(entry), flush=True)
            cache_.save()

        for cap in args.thresholds:
            evaluate(f"split@{cap}", [chunk_texts(f, cap, per_char) for f in functions], cache)
    finally:
        embedder.close()

    if not args.skip_truncate:
        # The naive alternative: one chunk per function, cut off at 512 tokens.
        short = embed_backends.load(args.config, n_ctx=512)
        try:
            evaluate("truncate@512", [chunk_texts(f, 10**9, per_char) for f in functions], Cache(short, args.config + "_ctx512"))
        finally:
            short.close()

    # Paired comparison against keeping functions whole: all queries, and the long ones.
    base = all_ranks.get(f"split@{args.baseline}")
    if base is not None:
        long_mask = lengths > 256
        report["versus_baseline"] = {}
        for label, ranks in all_ranks.items():
            report["versus_baseline"][label] = {
                f"{metric} {scope}": paired_bootstrap(ranks[mask], base[mask], metric)
                for metric in ("recall@10", "mrr@10")
                for scope, mask in (("all", np.ones(len(base), dtype=bool)), ("over 256 tokens", long_mask))
            }
        print("versus_baseline", json.dumps(report["versus_baseline"]), flush=True)
    out = DATA / "results" / "cap_quality.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
