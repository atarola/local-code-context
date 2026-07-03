from __future__ import annotations

import sys
from typing import Any

from local_code_rag.syntax.detection import detect_language
from local_code_rag.syntax.legacy_python import (
    PythonSyntaxExtractor,
    extract_python_imports,
    extract_python_symbols,
)
from local_code_rag.syntax.rendering import (
    MAX_SIGNATURE_CHARS,
    build_structural_records,
    build_text_fallback_records,
    make_chunk_id,
)
from local_code_rag.syntax.extraction import (
    PythonTagQueryExtractor,
    TagQueryExtractor,
    compare_python_extractions,
)
from local_code_rag.syntax.models import (
    BuildResult,
    CodeImport,
    CodeSymbol,
    ComparisonGap,
    ExtractionResult,
    INDEX_SCHEMA_VERSION,
    IndexRecord,
    LanguageExtractor,
    ParseQuality,
)
from local_code_rag.syntax.parsers import ParserRegistry, get_parser_registry

try:  # pragma: no cover - optional dependency
    from tree_sitter import Tree
except Exception:  # pragma: no cover - optional dependency
    Tree = Any  # type: ignore[assignment]


MAX_PARSE_ERROR_RATIO = 0.35

EXTRACTORS: dict[str, LanguageExtractor] = {
    "python": PythonSyntaxExtractor(),
}

QUERY_EXTRACTORS: dict[str, TagQueryExtractor] = {
    "python": PythonTagQueryExtractor(),
    "rust": TagQueryExtractor("rust"),
}


def walk_tree(node: Any):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        children = list(getattr(current, "children", []) or [])
        stack.extend(reversed(children))


def _count_missing_nodes(node: Any) -> int:
    return sum(1 for child in walk_tree(node) if getattr(child, "is_missing", False))


def _count_error_nodes(node: Any) -> int:
    return sum(1 for child in walk_tree(node) if getattr(child, "type", "") == "ERROR")


def _error_bytes(node: Any) -> int:
    total = 0
    for child in walk_tree(node):
        if getattr(child, "type", "") == "ERROR" or getattr(child, "is_missing", False):
            total += max(
                0,
                int(getattr(child, "end_byte", 0))
                - int(getattr(child, "start_byte", 0)),
            )
    return total


def _has_meaningful_top_level_nodes(root: Any) -> bool:
    children = list(
        getattr(root, "named_children", []) or getattr(root, "children", []) or []
    )
    for child in children:
        if getattr(child, "type", "") not in {"ERROR", "comment"}:
            return True
    return False


def evaluate_parse_quality(tree: Any, source: bytes) -> ParseQuality:
    root = getattr(tree, "root_node", None)
    if root is None:
        return ParseQuality(False, 0, 0, len(source), 1.0 if source else 0.0)

    error_nodes = _count_error_nodes(root)
    missing_nodes = _count_missing_nodes(root)
    error_bytes = min(len(source), _error_bytes(root))
    error_ratio = error_bytes / len(source) if source else 0.0
    usable = (
        error_ratio <= MAX_PARSE_ERROR_RATIO
        and _has_meaningful_top_level_nodes(root)
        and (error_nodes + missing_nodes)
        < max(1, len(list(getattr(root, "children", []) or [])))
    )
    return ParseQuality(usable, error_nodes, missing_nodes, error_bytes, error_ratio)


def _extractor_for_language(language: str) -> LanguageExtractor | None:
    return EXTRACTORS.get(language.lower())


def _query_extractor_for_language(language: str) -> TagQueryExtractor | None:
    return QUERY_EXTRACTORS.get(language.lower())


def _build_query_records(
    *,
    language: str,
    repo: str,
    repo_root: Any,
    relative_path: str,
    source: bytes,
    text: str,
    python_extractor_mode: str = "legacy",
    parser_registry: ParserRegistry | None = None,
) -> BuildResult:
    del python_extractor_mode
    registry = parser_registry or get_parser_registry()
    parser = registry.get(language)
    if parser is None:
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language=language,
                reason=f"{language} parser unavailable",
            ),
            language=language,
            fallback_reason=f"{language} parser unavailable",
        )

    query_extractor = _query_extractor_for_language(language)
    if query_extractor is None:
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language=language,
                reason=f"{language} extractor unavailable",
            ),
            language=language,
            fallback_reason=f"{language} extractor unavailable",
        )

    try:
        tree = parser.parse(source)
    except Exception as exc:
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language=language,
                reason=f"{language} parser failed: {exc}",
            ),
            language=language,
            fallback_reason=str(exc),
        )

    quality = evaluate_parse_quality(tree, source)
    if not quality.usable:
        reason = (
            "parse quality unusable "
            f"(errors={quality.error_nodes} missing={quality.missing_nodes} "
            f"error_ratio={quality.error_ratio:.2f})"
        )
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language=language,
                reason=reason,
            ),
            language=language,
            fallback_reason=reason,
        )

    try:
        extraction = query_extractor.extract(source, tree, relative_path)
    except Exception as exc:
        print(
            f"{language} tags query failed for {repo}:{relative_path}: {exc}; falling back to text chunks",
            file=sys.stderr,
        )
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language=language,
                reason=f"{language} query failed: {exc}",
            ),
            language=language,
            fallback_reason=str(exc),
        )

    if not extraction.symbols:
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language=language,
                reason=f"no useful {language} symbols extracted",
            ),
            language=language,
            fallback_reason=f"no useful {language} symbols extracted",
        )

    records = build_structural_records(
        repo=repo,
        repo_root=repo_root,
        relative_path=relative_path,
        language=language,
        source=source,
        symbols=extraction.symbols,
        imports=extraction.imports,
    )
    return BuildResult(records=records, language=language, fallback_reason=None)


def _compare_gaps_to_text(gaps: list[ComparisonGap]) -> None:
    for gap in gaps:
        print(
            f"python query parity gap ({gap.field}): "
            f"legacy={list(gap.legacy)} query={list(gap.query)}",
            file=sys.stderr,
        )


def _build_python_records(
    *,
    language: str = "python",
    repo: str,
    repo_root: Any,
    relative_path: str,
    source: bytes,
    text: str,
    parser_registry: ParserRegistry | None = None,
    python_extractor_mode: str = "legacy",
) -> BuildResult:
    del language
    registry = parser_registry or get_parser_registry()
    parser = registry.get("python")
    if parser is None:
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language="python",
                reason="python parser unavailable",
            ),
            language="python",
            fallback_reason="python parser unavailable",
        )

    legacy_extractor = _extractor_for_language("python")
    if legacy_extractor is None:
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language="python",
                reason="python extractor unavailable",
            ),
            language="python",
            fallback_reason="python extractor unavailable",
        )

    try:
        tree = parser.parse(source)
    except Exception as exc:
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language="python",
                reason=f"python parser failed: {exc}",
            ),
            language="python",
            fallback_reason=str(exc),
        )

    quality = evaluate_parse_quality(tree, source)
    if not quality.usable:
        reason = (
            "parse quality unusable "
            f"(errors={quality.error_nodes} missing={quality.missing_nodes} "
            f"error_ratio={quality.error_ratio:.2f})"
        )
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language="python",
                reason=reason,
            ),
            language="python",
            fallback_reason=reason,
        )

    mode = python_extractor_mode.lower()
    if mode not in {"legacy", "query", "compare"}:
        mode = "legacy"

    if mode == "legacy":
        try:
            extraction = legacy_extractor.extract(source, tree, relative_path)
        except Exception as exc:
            return BuildResult(
                records=build_text_fallback_records(
                    repo=repo,
                    repo_root=repo_root,
                    relative_path=relative_path,
                    text=text,
                    language="python",
                    reason=f"python extractor failed: {exc}",
                ),
                language="python",
                fallback_reason=str(exc),
            )

        if not extraction.symbols:
            return BuildResult(
                records=build_text_fallback_records(
                    repo=repo,
                    repo_root=repo_root,
                    relative_path=relative_path,
                    text=text,
                    language="python",
                    reason="no useful Python symbols extracted",
                ),
                language="python",
                fallback_reason="no useful Python symbols extracted",
            )

        records = build_structural_records(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            language="python",
            source=source,
            symbols=extraction.symbols,
            imports=extraction.imports,
        )
        return BuildResult(records=records, language="python", fallback_reason=None)

    query_extractor = _query_extractor_for_language("python")
    if query_extractor is None:
        print(
            f"python tags query unavailable for {repo}:{relative_path}; falling back to legacy extractor",
            file=sys.stderr,
        )
        return _build_python_records(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            source=source,
            text=text,
            parser_registry=parser_registry,
            python_extractor_mode="legacy",
        )

    try:
        comparison = compare_python_extractions(
            source,
            tree,
            relative_path,
            repo=repo,
            repo_root=repo_root,
            capture_source=query_extractor._capture_source_or_default(),  # type: ignore[attr-defined]
        )
    except Exception as exc:
        print(
            f"python tags query failed for {repo}:{relative_path}: {exc}; falling back to legacy extractor",
            file=sys.stderr,
        )
        return _build_python_records(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            source=source,
            text=text,
            parser_registry=parser_registry,
            python_extractor_mode="legacy",
        )

    if not comparison.legacy.symbols:
        return BuildResult(
            records=build_text_fallback_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                text=text,
                language="python",
                reason="no useful Python symbols extracted",
            ),
            language="python",
            fallback_reason="no useful Python symbols extracted",
        )

    if comparison.gaps:
        _compare_gaps_to_text(comparison.gaps)
        if mode == "query":
            return _build_python_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                source=source,
                text=text,
                parser_registry=parser_registry,
                python_extractor_mode="legacy",
            )

    if not comparison.query.symbols:
        print(
            f"python tags query produced no symbols for {repo}:{relative_path}; falling back to legacy extractor",
            file=sys.stderr,
        )
        return _build_python_records(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            source=source,
            text=text,
            parser_registry=parser_registry,
            python_extractor_mode="legacy",
        )

    if mode == "compare":
        return BuildResult(
            records=comparison.legacy_records, language="python", fallback_reason=None
        )

    return BuildResult(
        records=comparison.query_records,
        language="python",
        fallback_reason=None,
    )


def build_index_records(
    *,
    repo: str,
    repo_root: Any,
    path: Any,
    source: bytes,
    text: str,
    parser_registry: ParserRegistry | None = None,
    python_extractor_mode: str = "legacy",
) -> BuildResult:
    relative_path = path.relative_to(repo_root).as_posix()
    language = detect_language(path, source, repository_hints={repo})
    builders: dict[str, Any] = {
        "python": _build_python_records,
        "rust": _build_query_records,
    }
    builder = builders.get(language or "")
    if builder is not None:
        return builder(
            language=language or "",
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            source=source,
            text=text,
            python_extractor_mode=python_extractor_mode,
            parser_registry=parser_registry,
        )

    reason = (
        "unknown language" if language is None else f"unsupported language {language}"
    )
    return BuildResult(
        records=build_text_fallback_records(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            text=text,
            language=language or "",
            reason=reason,
        ),
        language=language or "",
        fallback_reason=reason,
    )


__all__ = [
    "BuildResult",
    "CodeImport",
    "CodeSymbol",
    "ComparisonGap",
    "ExtractionResult",
    "INDEX_SCHEMA_VERSION",
    "IndexRecord",
    "MAX_SIGNATURE_CHARS",
    "ParseQuality",
    "build_index_records",
    "compare_python_extractions",
    "evaluate_parse_quality",
    "extract_python_imports",
    "extract_python_symbols",
    "make_chunk_id",
    "PythonTagQueryExtractor",
    "walk_tree",
]
