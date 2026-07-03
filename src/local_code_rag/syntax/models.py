from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


INDEX_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class ParseQuality:
    usable: bool
    error_nodes: int
    missing_nodes: int
    error_bytes: int
    error_ratio: float


@dataclass(frozen=True)
class CodeSymbol:
    name: str
    kind: str
    language: str
    path: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str | None
    parent: str | None
    exported: bool | None


@dataclass(frozen=True)
class CodeImport:
    source: str
    imported_names: tuple[str, ...]
    path: str
    start_line: int


@dataclass(frozen=True)
class ExtractionResult:
    symbols: list[CodeSymbol]
    imports: list[CodeImport]


@dataclass(frozen=True)
class IndexRecord:
    id: str
    document: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class BuildResult:
    records: list[IndexRecord]
    language: str
    fallback_reason: str | None = None


@dataclass(frozen=True)
class ComparisonGap:
    field: str
    legacy: tuple[str, ...]
    query: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionComparison:
    legacy: ExtractionResult
    query: ExtractionResult
    legacy_records: list[IndexRecord]
    query_records: list[IndexRecord]
    gaps: list[ComparisonGap]


class LanguageExtractor(Protocol):
    def extract(
        self, source: bytes, tree: Any, relative_path: str
    ) -> ExtractionResult: ...
