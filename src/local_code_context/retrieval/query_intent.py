from __future__ import annotations

import re
from typing import Literal

QueryIntent = Literal[
    "exact_symbol",
    "implementation",
    "usage",
    "orientation",
    "fallback",
    "general",
]

_EXACT_SYMBOL_PATTERNS = [
    re.compile(r"\bfind\s+(?:the\s+)?(?:function|class|method|label|symbol|constant)\s+(.+?)[?.]?\s*$", re.I),
    re.compile(r"\bwhere\s+is\s+(.+?)\s+defined[?.]?\s*$", re.I),
    re.compile(r"\bshow\s+(?:me\s+)?(?:the\s+)?(?:function|class|method|label|symbol|constant)\s+(.+?)[?.]?\s*$", re.I),
    re.compile(r"\bwhere\s+can\s+I\s+find\s+(.+?)[?.]?\s*$", re.I),
]

_IMPLEMENTATION_PATTERNS = [
    re.compile(r"\bhow\s+does\s+", re.I),
    re.compile(r"\bhow\s+is\s+", re.I),
    re.compile(r"\bhow\s+are\s+", re.I),
    re.compile(r"\bhow\s+to\s+", re.I),
    re.compile(r"\bhow\b.*\bwork(s)?\b", re.I),
    re.compile(r"\bprocess(?:es)?\s+", re.I),
    re.compile(r"\bimplement(?:ed|ation|s)?\b", re.I),
    re.compile(r"\bmechanism\b", re.I),
]

_USAGE_PATTERNS = [
    re.compile(r"\bwhere\s+is\s+.{1,60}\s+used\b", re.I),
    re.compile(r"\bwhat\s+calls\s+", re.I),
    re.compile(r"\bwho\s+calls\s+", re.I),
    re.compile(r"\busages?\s+of\s+", re.I),
    re.compile(r"\breferences?\s+to\s+", re.I),
]

_ORIENTATION_PATTERNS = [
    re.compile(r"\bwhat\s+(?:are\s+)?the\s+(?:major\s+)?(?:subsystems?|components?|modules?|parts?)\b", re.I),
    re.compile(r"\barchitecture\b", re.I),
    re.compile(r"\boverview\b", re.I),
    re.compile(r"\bhow\s+(?:is|are)\s+.{1,60}\s+(?:organized|structured|laid out)\b", re.I),
    re.compile(r"\bwhat\s+(?:does|do)\s+.{1,60}\s+do\b", re.I),
]

_FALLBACK_PATTERNS = [
    re.compile(r"\bpin\s+assignment", re.I),
    re.compile(r"\bpin\s+layout", re.I),
    re.compile(r"\bconstants?\s+define", re.I),
    re.compile(r"\bvector\s+address", re.I),
    re.compile(r"\bconfiguration\s+(?:file|setup|value)", re.I),
]

_IDENTIFIER_RE = re.compile(r"[`'\"]?([A-Za-z_]\w*(?:\.\w+)*)[`'\"]?")


def classify_query(query: str) -> QueryIntent:
    cleaned = query.strip()
    if not cleaned:
        return "general"

    for pat in _EXACT_SYMBOL_PATTERNS:
        if pat.search(cleaned):
            return "exact_symbol"

    for pat in _FALLBACK_PATTERNS:
        if pat.search(cleaned):
            return "fallback"

    for pat in _ORIENTATION_PATTERNS:
        if pat.search(cleaned):
            return "orientation"

    for pat in _USAGE_PATTERNS:
        if pat.search(cleaned):
            return "usage"

    for pat in _IMPLEMENTATION_PATTERNS:
        if pat.search(cleaned):
            return "implementation"

    return "general"


def extract_identifiers(query: str) -> list[str]:
    raw: list[str] = []
    for m in _IDENTIFIER_RE.finditer(query):
        ident = m.group(1)
        if ident.lower() not in {
            "the", "find", "where", "show", "what", "how", "are", "is",
            "does", "do", "can", "i", "me", "function", "class", "method",
            "label", "symbol", "constant", "defined", "used", "work", "works",
            "process", "processes",
        }:
            raw.append(ident)
    return raw
