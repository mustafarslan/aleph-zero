"""Parsers, one per grammar, created on first use."""
from __future__ import annotations

import time

import tree_sitter_language_pack as pack
from tree_sitter import Language, Parser, Tree

from .languages import PROFILES
from .profile import LanguageProfile

_PARSERS: dict[str, Parser] = {}
_LANGUAGES: dict[str, Language] = {}
_FAILED: dict[str, tuple[float, Exception]] = {}   # grammar -> when it last failed to load, and how
RETRY_SECONDS = 60.0     # a grammar that could not load (offline, a proxy) is asked for again after this, not per file
announce = None          # told the grammars about to be downloaded (the command line sets it on a terminal)


def parser(profile: LanguageProfile) -> Parser:
    found = _PARSERS.get(profile.grammar)
    if found is None:
        failed = _FAILED.get(profile.grammar)
        if failed is not None and time.monotonic() - failed[0] < RETRY_SECONDS:
            raise failed[1]
        try:
            found = _PARSERS[profile.grammar] = pack.get_parser(profile.grammar)
        except Exception as exc:  # noqa: BLE001 - whatever the download or the load ran into
            _FAILED[profile.grammar] = (time.monotonic(), exc)
            raise
        _FAILED.pop(profile.grammar, None)
    return found


def fetch_missing(grammars: set[str]) -> None:
    """Download the grammars of these that are not here yet, in this process and before any file is parsed: one
    download of only what the files need, said first, instead of a silent wait inside the first file of each language."""
    now = time.monotonic()
    wanted = {name for name in grammars
              if name not in _PARSERS and not (name in _FAILED and now - _FAILED[name][0] < RETRY_SECONDS)}
    if not wanted:
        return
    try:
        wanted -= set(pack.downloaded_languages())
    except Exception:  # noqa: BLE001 - the cache cannot be listed: each grammar is then fetched on first use
        return
    if not wanted:
        return
    if announce is not None:
        announce(sorted(wanted))
    try:
        pack.download(sorted(wanted))
    except Exception as exc:  # noqa: BLE001 - offline, a proxy: the files are read by windows and read again later
        for name in wanted:
            _FAILED[name] = (time.monotonic(), exc)


def failed() -> dict[str, str]:
    """The grammars that could not load in this process, and why."""
    return {grammar: str(exc) for grammar, (_, exc) in _FAILED.items()}


def loads(profile: LanguageProfile) -> bool:
    """Whether the grammar is there to parse with (fetched on first use, as for any file)."""
    try:
        parser(profile)
    except Exception:  # noqa: BLE001
        return False
    return True


def forget_parsers() -> None:
    """Drop the parsers (a session over its memory share). They are made again on first use;
    the grammars themselves stay loaded."""
    _PARSERS.clear()


def language(profile: LanguageProfile) -> Language:
    found = _LANGUAGES.get(profile.grammar)
    if found is None:
        found = _LANGUAGES[profile.grammar] = pack.get_language(profile.grammar)
    return found


def tree(profile: LanguageProfile, data: bytes) -> Tree:
    return parser(profile).parse(data)


def ensure_grammars() -> int:
    """Download every grammar the profiles need, so the first search does not wait for
    one. Returns how many were fetched."""
    return pack.download(sorted({profile.grammar for profile in PROFILES.values()}))
