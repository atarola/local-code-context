from __future__ import annotations

import hashlib
import sys
from functools import lru_cache
from importlib.resources import files
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from local_code_rag.syntax.parsers import get_parser_registry
from local_code_rag.syntax.legacy_python import (
    _assignment_name,
    _collapse_signature,
    _definition_name,
    _definition_node,
    _extract_python_import_names,
    _is_useful_constant,
    _node_lines,
    _node_text,
)
from local_code_rag.syntax.rendering import build_structural_records
from local_code_rag.syntax.models import (
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

QUERY_RESOURCES = {
    "python": "python-tags.scm",
    "rust": "rust-tags.scm",
}

SYMBOL_KIND_PRIORITY = {
    "module": 0,
    "class": 1,
    "struct": 1,
    "enum": 1,
    "union": 1,
    "trait": 1,
    "type": 1,
    "method": 2,
    "function": 3,
    "constant": 4,
    "impl": 5,
    "macro": 6,
}


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
    normalize_symbol: Callable[[CapturedSymbol, ExtractionContext], CapturedSymbol] | None = (
        None
    )
    normalize_import: Callable[[CapturedImport, ExtractionContext], CapturedImport] | None = (
        None
    )
    postprocess: Callable[
        [ExtractionContext, list[CapturedSymbol], list[CapturedImport]],
        tuple[list[CapturedSymbol], list[CapturedImport]],
    ] | None = None


@lru_cache(maxsize=None)
def load_tags_query(language: str) -> str | None:
    resource_name = QUERY_RESOURCES.get(language.lower())
    if resource_name is None:
        return None

    try:
        return (
            files("local_code_rag.syntax.queries")
            .joinpath(resource_name)
            .read_text(encoding="utf-8")
        )
    except OSError as exc:
        print(
            f"failed to read {language} tags query: {exc}",
            file=sys.stderr,
        )
        return None


def load_python_tags_query() -> str | None:
    return load_tags_query("python")


def load_rust_tags_query() -> str | None:
    return load_tags_query("rust")


def _load_language(language: str) -> Any | None:
    registry = get_parser_registry()
    try:
        return registry.get_language(language)
    except Exception as exc:  # pragma: no cover - defensive
        print(
            f"failed to load tree-sitter language for {language}: {exc}",
            file=sys.stderr,
        )
        return None


def _compile_query(language_name: str, language: Any, query_source: str) -> Any | None:
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
        print(
            f"failed to compile {language_name} tags query: {exc}",
            file=sys.stderr,
        )
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
    if normalized in {"definition.class", "definition.struct"}:
        return "class"
    if normalized == "definition.enum":
        return "enum"
    if normalized == "definition.union":
        return "union"
    if normalized in {"definition.interface", "definition.trait"}:
        return "trait"
    if normalized == "definition.type":
        return "type"
    if normalized in {"definition.constant", "definition.static"}:
        return "constant"
    if normalized == "definition.module":
        return "module"
    if normalized == "definition.method":
        return "method"
    if normalized == "definition.function":
        return "function"
    if normalized == "definition.impl":
        return "impl"
    if normalized in {"reference.import", "definition.import"}:
        return "import"
    if normalized == "reference.call":
        return "call"
    if normalized == "name":
        return "name"
    return None


def _is_name_capture(name: str) -> bool:
    lowered = name.lower()
    return lowered == "name" or lowered.startswith("name.")


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


@lru_cache(maxsize=None)
def _compile_tag_capture_source(language: str) -> CaptureSource | None:
    query_source = load_tags_query(language)
    if not query_source:
        return None
    language_obj = _load_language(language)
    compiled_query = _compile_query(language, language_obj, query_source)
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
                    f"failed to run {language} tags query: {exc}",
                    file=sys.stderr,
                )
                return []
            return raw

    return _RuntimeCaptureSource()


def _normalize_python_symbol(
    symbol: CapturedSymbol, context: ExtractionContext
) -> CapturedSymbol:
    if symbol.kind != "function":
        return symbol
    class_node = _nearest_ancestor(context.node, {"class_definition"})
    if class_node is None:
        return symbol
    class_name = _definition_name(_node_text(context.source, class_node), "class")
    if class_name is None:
        return symbol
    return CapturedSymbol(
        name=symbol.name,
        kind="method",
        language=symbol.language,
        path=symbol.path,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
        start_byte=symbol.start_byte,
        end_byte=symbol.end_byte,
        signature=symbol.signature,
        parent=class_name,
        exported=symbol.exported,
        node=symbol.node,
    )


def _augment_python_query_result(
    context: ExtractionContext,
    symbols: list[CapturedSymbol],
    imports: list[CapturedImport],
) -> tuple[list[CapturedSymbol], list[CapturedImport]]:
    existing = {
        (symbol.start_byte, symbol.end_byte, symbol.kind, symbol.name, symbol.parent or "")
        for symbol in symbols
    }
    root = getattr(context.tree, "root_node", None)
    if root is None:
        return symbols, imports

    augmented = list(symbols)
    for node in list(
        getattr(root, "named_children", []) or getattr(root, "children", []) or []
    ):
        node_type = getattr(node, "type", "")
        if node_type not in {"assignment", "annotated_assignment"}:
            continue
        text = _node_text(context.source, node)
        name = _assignment_name(text)
        if not _is_useful_constant(text, name):
            continue
        start_line, end_line = _node_lines(node)
        start_byte = int(getattr(node, "start_byte", 0))
        end_byte = int(getattr(node, "end_byte", 0))
        candidate = CapturedSymbol(
            name=name,
            kind="constant",
            language=context.language,
            path=context.relative_path,
            start_line=start_line,
            end_line=end_line,
            start_byte=start_byte,
            end_byte=end_byte,
            signature=_collapse_signature(text.split("\n", 1)[0]),
            parent=None,
            exported=not name.startswith("_"),
            node=node,
        )
        key = (
            candidate.start_byte,
            candidate.end_byte,
            candidate.kind,
            candidate.name,
            candidate.parent or "",
        )
        if key not in existing:
            augmented.append(candidate)
            existing.add(key)

    return augmented, imports


QUERY_LANGUAGE_HOOKS: dict[str, QueryLanguageHooks] = {
    "python": QueryLanguageHooks(
        normalize_symbol=_normalize_python_symbol,
        postprocess=_augment_python_query_result,
    ),
}


class TagQueryExtractor:
    def __init__(
        self,
        language: str,
        capture_source: CaptureSource | None = None,
        *,
        hooks: QueryLanguageHooks | None = None,
    ) -> None:
        self.language = language.lower()
        self._capture_source = capture_source
        self._hooks = hooks

    def _capture_source_or_default(self) -> CaptureSource | None:
        if self._capture_source is not None:
            return self._capture_source
        if not self.language:
            return None
        return _compile_tag_capture_source(self.language)

    def extract(self, source: bytes, tree: Any, relative_path: str) -> ExtractionResult:
        capture_source = self._capture_source_or_default()
        if capture_source is None:
            raise RuntimeError(f"{self.language.title()} tags query unavailable")

        captures = _capture_pairs(tree, capture_source)
        if not captures:
            raise RuntimeError(f"{self.language.title()} tags query produced no captures")

        return _extract_result_from_captures(
            self.language,
            source,
            tree,
            relative_path,
            captures,
            hooks=self._hooks,
        )


class PythonTagQueryExtractor(TagQueryExtractor):
    def __init__(self, capture_source: CaptureSource | None = None) -> None:
        super().__init__("python", capture_source)


def _node_range(node: Any) -> tuple[int, int]:
    return int(getattr(node, "start_byte", 0)), int(getattr(node, "end_byte", 0))


def _node_contains(outer: Any, inner: Any) -> bool:
    outer_start, outer_end = _node_range(outer)
    inner_start, inner_end = _node_range(inner)
    return outer_start <= inner_start and inner_end <= outer_end


def _best_name_node(symbol_node: Any, name_nodes: list[Any]) -> Any | None:
    best: Any | None = None
    best_span = None
    for name_node in name_nodes:
        if not _node_contains(symbol_node, name_node):
            continue
        start, end = _node_range(name_node)
        span = end - start
        if best is None or best_span is None or span < best_span:
            best = name_node
            best_span = span
    return best


def _nearest_ancestor(node: Any, types: set[str]) -> Any | None:
    current = _node_parent(node)
    while current is not None:
        if getattr(current, "type", "") in types:
            return current
        current = _node_parent(current)
    return None


def _rust_parent_from_text(text: str, node_type: str) -> str | None:
    if node_type == "trait_item":
        name = _definition_name(text, "class")
        if name is not None:
            return name
        return None

    header = text.split("{", 1)[0].split(" where ", 1)[0].strip()
    header = header.removeprefix("default ").strip()
    header = header.removeprefix("unsafe ").strip()
    header = header.removeprefix("pub ").strip()
    if header.startswith("impl "):
        header = header[len("impl ") :].strip()
    return header or None


def _dedupe_symbols(symbols: list[CapturedSymbol]) -> list[CapturedSymbol]:
    selected: dict[tuple[int, int], CodeSymbol] = {}
    priorities: dict[tuple[int, int], int] = {}
    for symbol in symbols:
        key = (symbol.start_byte, symbol.end_byte)
        priority = SYMBOL_KIND_PRIORITY.get(symbol.kind, 99)
        if key not in selected or priority < priorities[key]:
            selected[key] = symbol
            priorities[key] = priority
    return sorted(selected.values(), key=_symbol_key)


def _capture_to_kind(capture_name: str, node: Any) -> str | None:
    category = _capture_category(capture_name)
    if category is None or category in {"import", "call", "name", "module"}:
        return None
    if category == "class":
        node_type = getattr(node, "type", "")
        if node_type == "type_item":
            return "type"
        if node_type == "trait_item":
            return "trait"
        return "class"
    if category in {
        "struct",
        "enum",
        "union",
        "trait",
        "type",
        "constant",
        "module",
        "method",
        "function",
        "impl",
        "macro",
    }:
        return category
    return None


def _capture_to_name(category: str, node: Any, source: bytes, *, name_nodes: list[Any]) -> str | None:
    name_node = _best_name_node(node, name_nodes)
    candidate = name_node if name_node is not None else node
    text = _node_text(source, candidate).strip()
    if not text:
        return None

    if category == "module":
        import re

        match = re.search(r"^\s*mod\s+([A-Za-z_][A-Za-z0-9_]*)", text)
        if match:
            return match.group(1)
    if category in {"class", "struct", "enum", "union", "trait", "type", "constant", "macro"}:
        import re

        pattern = {
            "class": r"^\s*(?:class|struct|enum|union|trait|type)\s+([A-Za-z_][A-Za-z0-9_]*)",
            "struct": r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)",
            "enum": r"^\s*(?:pub\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)",
            "union": r"^\s*(?:pub\s+)?union\s+([A-Za-z_][A-Za-z0-9_]*)",
            "trait": r"^\s*(?:pub\s+)?trait\s+([A-Za-z_][A-Za-z0-9_]*)",
            "type": r"^\s*(?:pub\s+)?type\s+([A-Za-z_][A-Za-z0-9_]*)",
            "constant": r"^\s*(?:pub\s+)?(?:const|static)\s+([A-Za-z_][A-Za-z0-9_]*)",
            "macro": r"^\s*(?:pub\s+)?macro_rules!\s+([A-Za-z_][A-Za-z0-9_]*)",
        }[category]
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    if category in {"function", "method"}:
        import re

        match = re.search(
            r"^\s*(?:pub\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]+\"\s+)?(?:fn|def|function)\s+([A-Za-z_][A-Za-z0-9_]*)",
            text,
        )
        if match:
            return match.group(1)
    if category == "impl":
        header = text.split("{", 1)[0].split(" where ", 1)[0].strip()
        header = header.removeprefix("default ").strip()
        header = header.removeprefix("unsafe ").strip()
        header = header.removeprefix("pub ").strip()
        if header.startswith("impl "):
            header = header[len("impl ") :].strip()
        return header or None
    if category == "constant":
        import re

        name = _assignment_name(text)
        if name is None:
            match = re.search(r"^([A-Za-z_][A-Za-z0-9_]*)$", text)
            if match:
                name = match.group(1)
        if not _is_useful_constant(text, name):
            return None
        return name
    return text.split()[0] if text.split() else None


def _capture_to_signature(category: str, text: str) -> str | None:
    if category == "impl":
        signature = text.split("{", 1)[0].split(" where ", 1)[0].strip()
    else:
        signature = text.split("{", 1)[0].split(";", 1)[0].strip()
    signature = _collapse_signature(signature)
    return signature or None


def _capture_to_parent(
    category: str,
    node: Any,
    source: bytes,
) -> str | None:
    if category == "method":
        class_node = _nearest_ancestor(node, {"class_definition", "impl_item", "trait_item"})
        if class_node is None:
            return None
        parent_text = _node_text(source, class_node)
        parent_type = getattr(class_node, "type", "")
        if parent_type == "class_definition":
            return _definition_name(parent_text, "class")
        if parent_type in {"impl_item", "trait_item"}:
            return _rust_parent_from_text(parent_text, parent_type)
    return None


def _capture_to_symbol(
    capture_name: str,
    node: Any,
    source: bytes,
    relative_path: str,
    *,
    language: str,
    name_nodes: list[Any],
) -> CapturedSymbol | None:
    category = _capture_to_kind(capture_name, node)
    if category is None:
        return None

    definition = _definition_node(node)
    text = _node_text(source, definition)
    name = _capture_to_name(category, definition, source, name_nodes=name_nodes)
    if name is None:
        return None

    if category == "function":
        parent = _capture_to_parent("method", definition, source)
        kind = "method" if parent is not None else "function"
    elif category == "method":
        kind = "method"
        parent = _capture_to_parent("method", definition, source)
    else:
        kind = category
        parent = None

    start_line, end_line = _node_lines(node)
    exported = not name.startswith("_")
    return CapturedSymbol(
        name=name,
        kind=kind,
        language=language,
        path=relative_path,
        start_line=start_line,
        end_line=end_line,
        start_byte=int(getattr(node, "start_byte", 0)),
        end_byte=int(getattr(node, "end_byte", 0)),
        signature=_capture_to_signature(kind, text),
        parent=parent,
        exported=exported,
        node=node,
    )


def _capture_to_import(
    node: Any, source: bytes, relative_path: str
) -> CapturedImport | None:
    text = _node_text(source, node).strip()
    if not text:
        return None
    if text.startswith("from "):
        source_name = text.split(" import ", 1)[0][len("from ") :].strip()
        imported = _extract_python_import_names(text)
    elif text.startswith("import "):
        source_name = text[len("import ") :].strip()
        imported = _extract_python_import_names(text)
    elif text.startswith("use "):
        source_name = text[len("use ") :].rstrip(";").strip()
        imported = ()
    else:
        source_name = text.rstrip(";").strip()
        imported = ()
    start_line, _ = _node_lines(node)
    return CapturedImport(
        source=source_name,
        imported_names=imported,
        path=relative_path,
        start_line=start_line,
        node=node,
    )


def _apply_symbol_hook(
    symbol: CapturedSymbol, context: ExtractionContext, hooks: QueryLanguageHooks
) -> CapturedSymbol:
    if hooks.normalize_symbol is None:
        return symbol
    return hooks.normalize_symbol(symbol, context)


def _apply_import_hook(
    item: CapturedImport, context: ExtractionContext, hooks: QueryLanguageHooks
) -> CapturedImport:
    if hooks.normalize_import is None:
        return item
    return hooks.normalize_import(item, context)


def _capture_to_generic_import(
    language: str,
    node: Any,
    source: bytes,
    relative_path: str,
    hooks: QueryLanguageHooks,
) -> CapturedImport | None:
    item = _capture_to_import(node, source, relative_path)
    if item is None:
        return None
    context = ExtractionContext(
        language=language,
        source=source,
        tree=None,
        relative_path=relative_path,
        node=node,
    )
    return _apply_import_hook(item, context, hooks)


def _symbols_from_captures(
    language: str,
    source: bytes,
    tree: Any,
    relative_path: str,
    captures: list[tuple[str, Any]],
    hooks: QueryLanguageHooks,
) -> tuple[list[CapturedSymbol], list[CapturedImport]]:
    symbol_captures: list[tuple[str, Any]] = []
    import_nodes: list[Any] = []
    name_nodes = [
        node for capture_name, node in captures if _is_name_capture(capture_name)
    ]
    for capture_name, node in captures:
        category = _capture_category(capture_name)
        if category == "import":
            import_nodes.append(node)
            continue
        if category == "call" or category == "name":
            continue
        symbol_captures.append((capture_name, node))

    symbols: list[CapturedSymbol] = []
    for capture_name, node in symbol_captures:
        symbol = _capture_to_symbol(
            capture_name,
            node,
            source,
            relative_path,
            language=language,
            name_nodes=name_nodes,
        )
        if symbol is None:
            continue
        context = ExtractionContext(
            language=language,
            source=source,
            tree=tree,
            relative_path=relative_path,
            node=node,
        )
        symbol = _apply_symbol_hook(symbol, context, hooks)
        if symbol is not None:
            symbols.append(symbol)

    imports = []
    for node in import_nodes:
        item = _capture_to_generic_import(language, node, source, relative_path, hooks)
        if item is not None:
            imports.append(item)
    return symbols, imports


def _dedupe_imports(imports: list[CapturedImport]) -> list[CapturedImport]:
    selected: dict[tuple[str, tuple[str, ...], int], CapturedImport] = {}
    for item in imports:
        key = (item.source, item.imported_names, item.start_line)
        if key not in selected:
            selected[key] = item
    return sorted(selected.values(), key=lambda item: (item.start_line, item.source))


def _to_code_symbol(item: CapturedSymbol) -> CodeSymbol:
    return CodeSymbol(
        name=item.name,
        kind=item.kind,
        language=item.language,
        path=item.path,
        start_line=item.start_line,
        end_line=item.end_line,
        start_byte=item.start_byte,
        end_byte=item.end_byte,
        signature=item.signature,
        parent=item.parent,
        exported=item.exported,
    )


def _to_code_import(item: CapturedImport) -> CodeImport:
    return CodeImport(
        source=item.source,
        imported_names=item.imported_names,
        path=item.path,
        start_line=item.start_line,
    )


def _extract_result_from_captures(
    language: str,
    source: bytes,
    tree: Any,
    relative_path: str,
    captures: list[tuple[str, Any]],
    *,
    hooks: QueryLanguageHooks | None = None,
) -> ExtractionResult:
    hooks = hooks or QUERY_LANGUAGE_HOOKS.get(language.lower(), QueryLanguageHooks())
    symbols, imports = _symbols_from_captures(
        language, source, tree, relative_path, captures, hooks
    )
    context = ExtractionContext(
        language=language,
        source=source,
        tree=tree,
        relative_path=relative_path,
        node=getattr(tree, "root_node", None),
    )
    if hooks.postprocess is not None:
        symbols, imports = hooks.postprocess(context, symbols, imports)
    deduped_symbols = _dedupe_symbols(symbols)
    deduped_imports = _dedupe_imports(imports)
    return ExtractionResult(
        symbols=[_to_code_symbol(item) for item in deduped_symbols],
        imports=[_to_code_import(item) for item in deduped_imports],
    )


def compare_python_extractions(
    source: bytes,
    tree: Any,
    relative_path: str,
    *,
    repo: str = "demo",
    repo_root: Path = Path("."),
    capture_source: CaptureSource | None = None,
) -> ExtractionComparison:
    from local_code_rag.syntax.legacy_python import PythonSyntaxExtractor

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
