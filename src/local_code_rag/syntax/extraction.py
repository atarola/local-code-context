from __future__ import annotations

from pathlib import Path
from typing import Any

from local_code_rag.syntax.capture_models import QueryLanguageHooks
from local_code_rag.syntax.capture_normalization import (
    PythonTagQueryExtractor,
    TagQueryExtractor,
    _comparison_gap,
    _imports_to_strings,
    _records_to_strings,
    _symbols_to_strings,
)
from local_code_rag.syntax.capture_normalization import load_tags_query as _load_tags_query
from local_code_rag.syntax.legacy_python import PythonSyntaxExtractor
from local_code_rag.syntax.models import (
    ComparisonGap,
    ExtractionComparison,
    ExtractionResult,
)
from local_code_rag.syntax.rendering import build_structural_records


def load_python_tags_query() -> str | None:
    return _load_tags_query("python")


def load_rust_tags_query() -> str | None:
    return _load_tags_query("rust")


def compare_python_extractions(
    source: bytes,
    tree: Any,
    relative_path: str,
    *,
    repo: str = "demo",
    repo_root: Path = Path("."),
    capture_source: Any | None = None,
) -> ExtractionComparison:
    legacy = PythonSyntaxExtractor().extract(source, tree, relative_path)
    query_extractor = PythonTagQueryExtractor(capture_source=capture_source)
    try:
        query = query_extractor.extract(source, tree, relative_path)
    except Exception as exc:
        query = ExtractionResult(symbols=[], imports=[])
        gaps = [
            ComparisonGap(
                field="query extractor",
                legacy=tuple(_symbols_to_strings(legacy.symbols)),
                query=(f"failed: {exc}",),
            )
        ]
        return ExtractionComparison(
            legacy=legacy,
            query=query,
            legacy_records=[],
            query_records=[],
            gaps=gaps,
        )

    legacy_records = build_structural_records(
        repo=repo,
        repo_root=repo_root,
        relative_path=relative_path,
        language="python",
        source=source,
        symbols=legacy.symbols,
        imports=legacy.imports,
    )
    query_records = build_structural_records(
        repo=repo,
        repo_root=repo_root,
        relative_path=relative_path,
        language="python",
        source=source,
        symbols=query.symbols,
        imports=query.imports,
    )

    gaps = [
        gap
        for gap in (
            _comparison_gap(
                "symbol names",
                _symbols_to_strings(legacy.symbols),
                _symbols_to_strings(query.symbols),
            ),
            _comparison_gap(
                "imports",
                _imports_to_strings(legacy.imports),
                _imports_to_strings(query.imports),
            ),
            _comparison_gap(
                "record ids",
                [record.id for record in legacy_records],
                [record.id for record in query_records],
            ),
            _comparison_gap(
                "rendered documents",
                _records_to_strings(legacy_records),
                _records_to_strings(query_records),
            ),
        )
        if gap is not None
    ]
    return ExtractionComparison(
        legacy=legacy,
        query=query,
        legacy_records=legacy_records,
        query_records=query_records,
        gaps=gaps,
    )


__all__ = [
    "ComparisonGap",
    "ExtractionComparison",
    "ExtractionResult",
    "PythonTagQueryExtractor",
    "QueryLanguageHooks",
    "TagQueryExtractor",
    "compare_python_extractions",
    "load_python_tags_query",
    "load_rust_tags_query",
    "load_tags_query",
]
