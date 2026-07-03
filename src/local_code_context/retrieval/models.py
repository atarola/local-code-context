from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HybridCandidate:
    record_id: str | None
    repo: str
    path: str
    language: str | None
    chunk_type: str | None
    symbol: str | None
    symbol_kind: str | None
    parent_symbol: str | None
    start_line: int | None
    end_line: int | None
    part_index: int | None
    document: str
    semantic_score: float = 0.0
    lexical_score: float = 0.0
    exact_symbol_score: float = 0.0
    match_sources: list[str] = field(default_factory=list)
    path_role: str = "unknown"


@dataclass
class HybridResult:
    id: str
    repo: str
    path: str
    language: str | None = None
    chunk_type: str | None = None
    symbol: str | None = None
    symbol_kind: str | None = None
    parent_symbol: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    part_index: int | None = None
    match_sources: list[str] = field(default_factory=list)
    semantic_score: float = 0.0
    lexical_score: float = 0.0
    exact_symbol_score: float = 0.0
    final_score: float = 0.0
    path_role: str = "unknown"
    document: str = ""


def composite_identity(meta: dict[str, Any]) -> tuple[Any, ...]:
    return (
        meta.get("repo", ""),
        meta.get("path", ""),
        meta.get("chunk_type", ""),
        meta.get("symbol", ""),
        meta.get("symbol_kind", ""),
        meta.get("parent_symbol", ""),
        meta.get("start_line"),
        meta.get("end_line"),
        meta.get("part_index"),
    )


def candidate_identity(c: HybridCandidate) -> tuple[Any, ...]:
    return (
        c.repo,
        c.path,
        c.chunk_type or "",
        c.symbol or "",
        c.symbol_kind or "",
        c.parent_symbol or "",
        c.start_line,
        c.end_line,
        c.part_index,
    )
