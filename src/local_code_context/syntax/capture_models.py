from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class CaptureSource(Protocol):
    def captures(self, tree: Any) -> Any: ...


@dataclass(frozen=True)
class ExtractionContext:
    language: str
    source: bytes
    tree: Any
    relative_path: str
    node: Any


@dataclass(frozen=True)
class CapturedSymbol:
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
    node: Any


@dataclass(frozen=True)
class CapturedImport:
    source: str
    imported_names: tuple[str, ...]
    path: str
    start_line: int
    node: Any


@dataclass(frozen=True)
class QueryLanguageHooks:
    normalize_symbol: Callable[
        [CapturedSymbol, ExtractionContext],
        CapturedSymbol | None,
    ] | None = None
    normalize_import: Callable[
        [CapturedImport, ExtractionContext],
        CapturedImport | None,
    ] | None = None
    postprocess: Callable[
        [ExtractionContext, list[CapturedSymbol], list[CapturedImport]],
        tuple[list[CapturedSymbol], list[CapturedImport]],
    ] | None = None

