from __future__ import annotations

import hashlib
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Protocol

from local_code_rag.python_syntax import (
    _assignment_name,
    _collapse_signature,
    _definition_name,
    _definition_node,
    _extract_python_import_names,
    _is_useful_constant,
    _node_lines,
    _node_text,
)
from local_code_rag.syntax_chunks import build_structural_records
from local_code_rag.syntax_models import (
    CodeImport,
    CodeSymbol,
    ComparisonGap,
    ExtractionComparison,
    ExtractionResult,
)

try:  # pragma: no cover - optional dependency
    from tree_sitter import Language, Query, QueryCursor
except Exception:  # pragma: no cover - optional dependency
    Language = None  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]
    QueryCursor = None  # type: ignore[assignment]

try:  # pragma: no cover - optional dependency
    import tree_sitter_python
except Exception:  # pragma: no cover - optional dependency
    tree_sitter_python = None  # type: ignore[assignment]


QUERY_FILE = Path(__file__).with_name("python_tags.scm")

SYMBOL_KIND_PRIORITY = {
    "module": 0,
    "class": 1,
    "method": 2,
    "function": 3,
    "constant": 4,
}


class CaptureSource(Protocol):
    def captures(self, tree: Any) -> Any: ...


@lru_cache(maxsize=None)
def load_tags_query(language: str) -> str | None:
    if language.lower() != "python":
        return None

    try:
        return QUERY_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"failed to read Python tags query: {exc}", file=sys.stderr)
        return None


def load_python_tags_query() -> str | None:
    return load_tags_query("python")


def _load_python_language() -> Any | None:
    if tree_sitter_python is None:
        return None

    language_factory = getattr(tree_sitter_python, "language", None)
    if language_factory is None:
        return None

    try:
        language = language_factory()
    except Exception as exc:
        print(f"failed to load Python tree-sitter language: {exc}", file=sys.stderr)
        return None

    if Language is not None and not isinstance(language, Language):
        try:
            language = Language(language)
        except Exception:
            pass
    return language


def _compile_query(language: Any, query_source: str) -> Any | None:
    if language is None or Query is None:
        return None

    if hasattr(language, "query"):
        try:
            return language.query(query_source)
        except Exception:
            pass

    try:
        return Query(language, query_source)
    except Exception as exc:
        print(f"failed to compile Python tags query: {exc}", file=sys.stderr)
        return None


def _normalize_capture_name(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("name."):
        lowered = lowered.removeprefix("name.")
    if lowered.startswith("definition.") or lowered.startswith("reference."):
        return lowered
    if lowered == "name":
        return "name"
    return lowered


def _capture_category(name: str) -> str | None:
    normalized = _normalize_capture_name(name)
    if "definition.class" in normalized:
        return "class"
    if "definition.method" in normalized:
        return "method"
    if "definition.function" in normalized:
        return "function"
    if "definition.module" in normalized:
        return "module"
    if normalized in {"reference.import", "definition.import"}:
        return "import"
    if normalized == "reference.call":
        return "call"
    if normalized == "name":
        return "name"
    return None


def _node_parent(node: Any) -> Any | None:
    return getattr(node, "parent", None)


def _nearest_enclosing_class(node: Any) -> Any | None:
    current = _node_parent(node)
    while current is not None:
        if getattr(current, "type", "") == "class_definition":
            return current
        current = _node_parent(current)
    return None


def _symbol_key(symbol: CodeSymbol) -> tuple[int, int, str, str, str]:
    return (
        symbol.start_byte,
        symbol.end_byte,
        symbol.kind,
        symbol.name,
        symbol.parent or "",
    )


def _symbols_to_strings(symbols: list[CodeSymbol]) -> list[str]:
    return [
        (
            f"{symbol.kind}:{symbol.name}:{symbol.parent or ''}:"
            f"{symbol.signature or ''}:{symbol.start_line}-{symbol.end_line}"
        )
        for symbol in symbols
    ]


def _imports_to_strings(imports: list[CodeImport]) -> list[str]:
    return [
        f"{item.source}:{','.join(item.imported_names)}:{item.start_line}"
        for item in imports
    ]


def _records_to_strings(records: list[Any]) -> list[str]:
    return [
        (
            f"{record.metadata.get('chunk_type', '')}:{record.metadata.get('symbol', '')}:"
            f"{record.metadata.get('symbol_kind', '')}:{record.metadata.get('parent_symbol', '')}:"
            f"{record.metadata.get('start_line', 0)}-{record.metadata.get('end_line', 0)}:"
            f"{record.id}:{hashlib.sha256(record.document.encode('utf-8')).hexdigest()}"
        )
        for record in records
    ]


def _comparison_gap(
    field: str, legacy: list[str], query: list[str]
) -> ComparisonGap | None:
    if legacy == query:
        return None
    return ComparisonGap(field=field, legacy=tuple(legacy), query=tuple(query))


def _capture_pairs(
    tree: Any, capture_source: CaptureSource | None
) -> list[tuple[str, Any]]:
    if capture_source is None:
        return []
    return _normalize_capture_pairs(capture_source.captures(tree))


def _normalize_capture_pairs(raw: Any) -> list[tuple[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        pairs: list[tuple[str, Any]] = []
        for name, nodes in raw.items():
            if isinstance(nodes, (list, tuple, set)):
                for node in nodes:
                    pairs.append((str(name), node))
            else:
                pairs.append((str(name), nodes))
        return pairs

    pairs = []
    for item in raw:
        if isinstance(item, tuple) and len(item) == 2:
            left, right = item
            if isinstance(left, str):
                pairs.append((left, right))
            elif isinstance(right, str):
                pairs.append((right, left))
            else:
                pairs.append((str(left), right))
        else:
            continue
    return pairs


@lru_cache(maxsize=1)
def _compile_python_capture_source() -> CaptureSource | None:
    query_source = load_python_tags_query()
    if not query_source:
        return None
    language = _load_python_language()
    compiled_query = _compile_query(language, query_source)
    if compiled_query is None:
        return None

    class _RuntimeCaptureSource:
        def captures(self, tree: Any) -> Iterable[tuple[str, Any]]:
            root = getattr(tree, "root_node", None)
            if root is None:
                return []
            if QueryCursor is None:
                return []
            try:
                cursor = QueryCursor(compiled_query)
                raw = cursor.captures(root)
            except Exception as exc:
                print(
                    f"failed to run Python tags query: {exc}",
                    file=sys.stderr,
                )
                return []
            return raw

    return _RuntimeCaptureSource()


class PythonTagQueryExtractor:
    language = "python"

    def __init__(self, capture_source: CaptureSource | None = None) -> None:
        self._capture_source = capture_source

    def _capture_source_or_default(self) -> CaptureSource | None:
        if self._capture_source is not None:
            return self._capture_source
        return _compile_python_capture_source()

    def extract(self, source: bytes, tree: Any, relative_path: str) -> ExtractionResult:
        capture_source = self._capture_source_or_default()
        if capture_source is None:
            raise RuntimeError("Python tags query unavailable")

        captures = _capture_pairs(tree, capture_source)
        if not captures:
            raise RuntimeError("Python tags query produced no captures")

        return _extract_python_result_from_captures(
            source, tree, relative_path, captures
        )


def _extract_imports_from_nodes(
    source: bytes, tree: Any, relative_path: str, import_nodes: list[Any]
) -> list[CodeImport]:
    imports: list[CodeImport] = []
    seen: set[tuple[str, tuple[str, ...], int]] = set()
    for node in import_nodes:
        text = _node_text(source, node).strip()
        if not text:
            continue
        if text.startswith("from "):
            source_name = text.split(" import ", 1)[0][len("from ") :].strip()
        elif text.startswith("import "):
            source_name = text[len("import ") :].strip()
        else:
            source_name = text
        imported = _extract_python_import_names(text)
        start_line, _ = _node_lines(node)
        key = (source_name, imported, start_line)
        if key in seen:
            continue
        seen.add(key)
        imports.append(
            CodeImport(
                source=source_name,
                imported_names=imported,
                path=relative_path,
                start_line=start_line,
            )
        )

    imports.sort(key=lambda item: (item.start_line, item.source))
    return imports


def _dedupe_symbols(symbols: list[CodeSymbol]) -> list[CodeSymbol]:
    selected: dict[tuple[int, int], CodeSymbol] = {}
    priorities: dict[tuple[int, int], int] = {}
    for symbol in symbols:
        key = (symbol.start_byte, symbol.end_byte)
        priority = SYMBOL_KIND_PRIORITY.get(symbol.kind, 99)
        if key not in selected or priority < priorities[key]:
            selected[key] = symbol
            priorities[key] = priority
    return sorted(selected.values(), key=_symbol_key)


def _symbol_from_capture(
    capture_name: str, node: Any, source: bytes, relative_path: str
) -> CodeSymbol | None:
    category = _capture_category(capture_name)
    if category is None or category == "import" or category == "call":
        return None

    definition = _definition_node(node)
    if category == "class":
        kind = "class"
    elif category == "method":
        kind = "method"
    elif category == "function":
        kind = (
            "method" if _nearest_enclosing_class(definition) is not None else "function"
        )
    elif category == "module":
        return None
    else:
        return None

    text = _node_text(source, definition)
    name = _definition_name(text, "class" if kind == "class" else "function")
    if name is None:
        return None

    parent = None
    if kind == "method":
        class_node = _nearest_enclosing_class(definition)
        if class_node is not None:
            class_text = _node_text(source, class_node)
            parent = _definition_name(class_text, "class")
    start_line, end_line = _node_lines(node)
    return CodeSymbol(
        name=name,
        kind=kind,
        language="python",
        path=relative_path,
        start_line=start_line,
        end_line=end_line,
        start_byte=int(getattr(node, "start_byte", 0)),
        end_byte=int(getattr(node, "end_byte", 0)),
        signature=_collapse_signature(text.split(":", 1)[0]) or None,
        parent=parent,
        exported=not name.startswith("_"),
    )


def _extract_constants_from_tree(
    source: bytes, tree: Any, relative_path: str
) -> list[CodeSymbol]:
    root = getattr(tree, "root_node", None)
    if root is None:
        return []
    constants: list[CodeSymbol] = []
    children = list(
        getattr(root, "named_children", []) or getattr(root, "children", []) or []
    )
    for node in children:
        node_type = getattr(node, "type", "")
        if node_type not in {"assignment", "annotated_assignment"}:
            continue
        text = _node_text(source, node)
        name = _assignment_name(text)
        if not _is_useful_constant(text, name):
            continue
        start_line, end_line = _node_lines(node)
        constants.append(
            CodeSymbol(
                name=name,
                kind="constant",
                language="python",
                path=relative_path,
                start_line=start_line,
                end_line=end_line,
                start_byte=int(getattr(node, "start_byte", 0)),
                end_byte=int(getattr(node, "end_byte", 0)),
                signature=_collapse_signature(text.split("\n", 1)[0]) or None,
                parent=None,
                exported=not name.startswith("_"),
            )
        )
    return constants


def _extract_python_result_from_captures(
    source: bytes,
    tree: Any,
    relative_path: str,
    captures: list[tuple[str, Any]],
) -> ExtractionResult:
    symbols_by_key: dict[tuple[int, int], CodeSymbol] = {}
    import_nodes: list[Any] = []

    for capture_name, node in captures:
        category = _capture_category(capture_name)
        if category == "import":
            import_nodes.append(node)
            continue
        symbol = _symbol_from_capture(capture_name, node, source, relative_path)
        if symbol is None:
            continue
        key = (symbol.start_byte, symbol.end_byte)
        current = symbols_by_key.get(key)
        if current is None:
            symbols_by_key[key] = symbol
            continue
        current_priority = SYMBOL_KIND_PRIORITY.get(current.kind, 99)
        new_priority = SYMBOL_KIND_PRIORITY.get(symbol.kind, 99)
        if new_priority < current_priority:
            symbols_by_key[key] = symbol

    symbols = _dedupe_symbols(
        list(symbols_by_key.values())
        + _extract_constants_from_tree(source, tree, relative_path)
    )
    imports = _extract_imports_from_nodes(source, tree, relative_path, import_nodes)
    return ExtractionResult(symbols=symbols, imports=imports)


def compare_python_extractions(
    source: bytes,
    tree: Any,
    relative_path: str,
    *,
    repo: str = "demo",
    repo_root: Path = Path("."),
    capture_source: CaptureSource | None = None,
) -> ExtractionComparison:
    from local_code_rag.python_syntax import PythonSyntaxExtractor

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
