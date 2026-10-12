"""Parsers, one per grammar, created on first use."""
from __future__ import annotations

import tree_sitter_language_pack as pack
from tree_sitter import Language, Parser, Tree

from .languages import PROFILES
from .profile import LanguageProfile

_PARSERS: dict[str, Parser] = {}
_LANGUAGES: dict[str, Language] = {}


def parser(profile: LanguageProfile) -> Parser:
    found = _PARSERS.get(profile.grammar)
    if found is None:
        found = _PARSERS[profile.grammar] = pack.get_parser(profile.grammar)
    return found


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
