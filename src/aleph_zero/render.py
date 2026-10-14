"""Search results as text for an agent, inside a token budget, and as JSON for programs.

The text is what an agent pays for on every search, so it spends the budget where it
helps most: every result gets its location line first, then text is added from the
best result down until the budget is used. Nothing is cut off silently: what did not
fit is counted in the last lines.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field

from .chunking import estimate_tokens
from .chunking.model import TOKENS_PER_CHAR
from .search import Hit

DEFAULT_BUDGET = 1500   # estimated tokens; the estimate is approximate for Claude's tokenizer
SNIPPET_LINES = 8
SHORT_SNIPPET_LINES = 3
ECHO_LIMIT = 80         # characters of the query repeated in a "no results" answer
DETAILS = ("locations", "snippets", "full")


@dataclass(frozen=True)
class Answer:
    query: str
    hits: list[Hit] = field(default_factory=list)
    pending: int = 0          # files of the view not indexed yet
    timed_out: bool = False
    elapsed_ms: int = 0
    # Semantic search: "" when it is switched off, "used" when vectors took part, otherwise
    # the reason they did not. `covered` and `chunks` say how much of the view has vectors.
    dense: str = ""
    covered: int = 0
    chunks: int = 0


@dataclass
class _Entry:
    """What one location line stands for: a hit, with the pieces that touch it merged
    in and the other files that hold the same text listed."""
    hit: Hit
    start: int
    end: int
    text: str
    also: list[Hit] = field(default_factory=list)


def as_json(answer: Answer) -> dict:
    return {
        "query": answer.query,
        "hits": [asdict(hit) for hit in answer.hits],
        "pending": answer.pending,
        "timed_out": answer.timed_out,
        "elapsed_ms": answer.elapsed_ms,
        "dense": answer.dense,
        "covered": answer.covered,
        "chunks": answer.chunks,
    }


def text(answer: Answer, budget_tokens: int = DEFAULT_BUDGET, detail: str = "snippets",
         already_shown: Callable[[Hit], bool] | None = None,
         printed: Callable[[Hit], None] | None = None) -> str:
    """The answer as text of at most `budget_tokens` estimated tokens.

    `detail` chooses what the budget is spent on: 'locations' (one or two lines per
    result), 'snippets' (the first lines of each), 'full' (whole chunks). With
    `already_shown`, text this session has seen before is replaced by a pointer,
    except under 'full', which is how a caller asks to see it again. `printed` is
    called for every hit whose text went into the answer."""
    if detail not in DETAILS:
        raise ValueError(f"detail must be one of {DETAILS}, not {detail!r}")
    notes = _notes(answer)
    if not answer.hits:
        asked = answer.query if len(answer.query) <= ECHO_LIMIT else answer.query[:ECHO_LIMIT].rstrip() + "…"
        return _within("\n".join([f'no results for "{asked}"', *notes]), budget_tokens)
    entries = _entries(answer.hits)
    heads = [_head(entry) for entry in entries]
    spent = estimate_tokens("\n".join(notes)) + (1 if notes else 0)

    def room(for_text: str, reserve: int) -> bool:
        return spent + estimate_tokens(for_text) + 1 + reserve <= budget_tokens

    # Every result's location first, best first, keeping room to say what was left out.
    shown = 0
    for position, head in enumerate(heads):
        left_out = _left_out(len(entries) - position - 1)
        if not room(head, estimate_tokens(left_out) + 1 if left_out else 0):
            break
        spent += estimate_tokens(head) + 1
        shown += 1
    omitted = _left_out(len(entries) - shown)
    if omitted:
        spent += estimate_tokens(omitted) + 1
    # Then text, from the best result down, while it fits.
    bodies: list[str] = [""] * shown
    if detail != "locations":
        for position in range(shown):
            entry = entries[position]
            pointer = detail != "full" and already_shown is not None and already_shown(entry.hit)
            if pointer:
                options = ["    (shown earlier in this session)"]
            elif detail == "full":
                options = [_body(entry.text, None), _body(entry.text, SNIPPET_LINES), _body(entry.text, SHORT_SNIPPET_LINES)]
            else:
                options = [_body(entry.text, SNIPPET_LINES), _body(entry.text, SHORT_SNIPPET_LINES)]
            chosen = next((option for option in options if room(option, 0)), None)
            if chosen is None:
                break  # lower results keep their location line only
            bodies[position] = chosen
            spent += estimate_tokens(chosen) + 1
            if printed is not None and not pointer:
                printed(entry.hit)
    lines: list[str] = []
    for position in range(shown):
        lines.append(heads[position])
        if bodies[position]:
            lines.append(bodies[position])
    if omitted:
        lines.append(omitted)
    return _within("\n".join([*lines, *notes]), budget_tokens)


def _within(text_out: str, budget_tokens: int) -> str:
    """The last line of defence for the ceiling: with a budget too small even for the
    notes, the text is cut and says so. Normal budgets never reach this."""
    if estimate_tokens(text_out) <= budget_tokens:
        return text_out
    room = max(0, int(budget_tokens / TOKENS_PER_CHAR) - 1)
    return text_out[:room].rstrip() + "…" if room else ""


def _notes(answer: Answer) -> list[str]:
    notes = []
    if answer.pending:
        notes.append(f"note: {answer.pending} files are not indexed yet; results may be incomplete")
    if answer.timed_out:
        notes.append(f"note: stopped at the time limit ({answer.elapsed_ms} ms); results may be incomplete")
    if answer.dense == "used" and answer.covered < answer.chunks:
        notes.append(f"note: semantic search covers {answer.covered} of {answer.chunks} chunks so far; "
                     "the rest are matched by words and names only")
    elif answer.dense not in ("", "used"):
        notes.append(f"note: semantic search was not used: {answer.dense}")
    return notes


def _left_out(count: int) -> str:
    if count <= 0:
        return ""
    return f"(+{count} more results not shown; narrow the query, add path_glob, or ask for detail=\"locations\")"


def _entries(hits: list[Hit]) -> list[_Entry]:
    entries: list[_Entry] = []
    by_text: dict[tuple, _Entry] = {}
    for hit in hits:
        same = by_text.get((hit.kind, hit.symbol, hit.breadcrumb, hit.text))
        if same is not None and same.hit.path != hit.path:
            same.also.append(hit)  # the same text in another file: listed, not repeated
            continue
        touching = next((entry for entry in entries
                         if entry.hit.path == hit.path and entry.hit.breadcrumb == hit.breadcrumb
                         and entry.hit.kind == hit.kind and hit.start_line <= entry.end + 1
                         and hit.end_line >= entry.start - 1), None)
        if touching is not None:
            before = hit.start_line < touching.start
            touching.text = hit.text + "\n" + touching.text if before else touching.text + "\n" + hit.text
            touching.start = min(touching.start, hit.start_line)
            touching.end = max(touching.end, hit.end_line)
            continue
        entry = _Entry(hit, hit.start_line, hit.end_line, hit.text)
        entries.append(entry)
        by_text.setdefault((hit.kind, hit.symbol, hit.breadcrumb, hit.text), entry)
    return entries


def _head(entry: _Entry) -> str:
    hit = entry.hit
    label = hit.breadcrumb or hit.symbol
    line = f"{hit.path}:{entry.start}-{entry.end}  {hit.kind}" + (f"  {label}" if label else "")
    if (hit.scope_start_line, hit.scope_end_line) != (hit.start_line, hit.end_line) and hit.scope_start_line:
        line += f"  (part of {hit.scope_start_line}-{hit.scope_end_line})"
    lines = [line]
    if hit.signature and hit.signature != label:
        lines.append(f"  {hit.signature}")
    if hit.places:
        lines.append("  also at: " + ", ".join(f"{start}-{end}" + (f" {name}" if name else "") for start, end, name in hit.places))
    if entry.also or hit.duplicates:
        places = ", ".join(f"{other.path}:{other.start_line}" for other in entry.also)
        more = f" (+{hit.duplicates} more files)" if hit.duplicates else ""
        lines.append(f"  also in: {places}{more}" if places else f"  also in {hit.duplicates} more files")
    return "\n".join(lines)


def _body(chunk_text: str, limit: int | None) -> str:
    lines = chunk_text.split("\n")
    kept = lines if limit is None or len(lines) <= limit else lines[:limit]
    out = ["    " + line if line else "" for line in kept]
    if len(kept) < len(lines):
        out.append(f"    … (+{len(lines) - len(kept)} lines)")
    return "\n".join(out)
