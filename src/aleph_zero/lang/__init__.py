"""Everything language-specific: what a definition is, how it is named, what an import looks like.

Ported from oxn v0.1.2 (github.com/mustafarslan/oxn, commit 2c8ee6d), MIT License,
Copyright (c) 2026 Mustafa Arslan. The profile design, the node-kind tables for
Python, TypeScript, JavaScript, Go, Rust and Java, the bound-name rule and the import
extractor come from there. Added here: C and C++, constants, variables and fields,
doc comments and signatures. oxn's metric tables are not part of the port.

This package imports nothing from the rest of aleph_zero, so it can be lifted into a
core that both projects share (ADR-007).
"""
from .languages import PROFILES, profile_for_path
from .profile import ImportSpec, LanguageProfile, Wrapper

__all__ = ["PROFILES", "ImportSpec", "LanguageProfile", "Wrapper", "profile_for_path"]
