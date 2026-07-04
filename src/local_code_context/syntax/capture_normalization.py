from __future__ import annotations

import hashlib
import sys
from functools import lru_cache
from typing import Any, Iterable

from local_code_context.syntax.capture_models import (
    CapturedImport,
    CapturedSymbol,
    CaptureSource,
    ExtractionContext,
    QueryLanguageHooks,
)
from local_code_context.syntax.hooks import QUERY_LANGUAGE_HOOKS
from local_code_context.syntax.models import CodeCall, CodeImport, CodeSymbol, ExtractionResult
from local_code_context.syntax.models import ComparisonGap
from local_code_context.syntax.parsers import get_parser_registry
from local_code_context.syntax.queries import load_tags_query
from local_code_context.syntax.legacy_python import (
    _assignment_name,
    _collapse_signature,
    _definition_node,
    _extract_python_import_names,
    _is_useful_constant,
    _node_lines,
    _node_text,
)

try:  # pragma: no cover - optional dependency
    from tree_sitter import Query, QueryCursor
except Exception:  # pragma: no cover - optional dependency
    Query = None  # type: ignore[assignment]
    QueryCursor = None  # type: ignore[assignment]


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
    if lowered == "name" or lowered.startswith("name."):
        return lowered
    if lowered.startswith("definition.") or lowered.startswith("reference."):
        return lowered
    return lowered


def _capture_category(name: str) -> str | None:
    normalized = _normalize_capture_name(name)
    if normalized == "name" or normalized.startswith("name."):
        return "name"
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
    return pairs


def _capture_pairs(tree: Any, capture_source: CaptureSource | None) -> list[tuple[str, Any]]:
    if capture_source is None:
        return []
    return _normalize_capture_pairs(capture_source.captures(tree))


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


def _capture_to_kind(capture_name: str, node: Any) -> str | None:
    category = _capture_category(capture_name)
    if category is None or category in {"import", "call", "name", "module"}:
        return None
    if category in {
        "class",
        "struct",
        "enum",
        "union",
        "trait",
        "type",
        "constant",
        "method",
        "function",
        "impl",
        "macro",
    }:
        return category
    return None


def _best_name_node(symbol_node: Any, name_nodes: list[Any]) -> Any | None:
    containers: list[Any] = [symbol_node]
    parent = getattr(symbol_node, "parent", None)
    if parent is not None:
        containers.append(parent)
        grandparent = getattr(parent, "parent", None)
        if grandparent is not None:
            containers.append(grandparent)

    for container in containers:
        best: Any | None = None
        best_score: tuple[int, int] | None = None
        for name_node in name_nodes:
            if not _node_contains(container, name_node):
                continue
            distance = 0
            current = name_node
            while current is not None and not _same_node(current, container):
                current = getattr(current, "parent", None)
                distance += 1
            if current is None:
                continue
            start, end = _node_range(name_node)
            score = (distance, end - start)
            if best is None or best_score is None or score < best_score:
                best = name_node
                best_score = score
        if best is not None:
            return best
    return None


def _node_range(node: Any) -> tuple[int, int]:
    return int(getattr(node, "start_byte", 0)), int(getattr(node, "end_byte", 0))


def _node_contains(outer: Any, inner: Any) -> bool:
    outer_start, outer_end = _node_range(outer)
    inner_start, inner_end = _node_range(inner)
    return outer_start <= inner_start and inner_end <= outer_end


def _same_node(left: Any, right: Any) -> bool:
    return (
        getattr(left, "type", "") == getattr(right, "type", "")
        and _node_range(left) == _node_range(right)
    )


def _capture_to_name(
    category: str, node: Any, source: bytes, *, name_nodes: list[Any]
) -> str | None:
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


def _capture_to_import(node: Any, source: bytes, relative_path: str) -> CapturedImport | None:
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


def _enclosing_def_name(node: Any, source: bytes) -> str | None:
    current = getattr(node, "parent", None)
    while current is not None:
        node_type = getattr(current, "type", "")
        if node_type in {"function_definition", "async_function_definition", "function_item"}:
            for child in getattr(current, "children", []):
                child_type = getattr(child, "type", "")
                if child_type in {"identifier", "name"}:
                    return _node_text(source, child).strip()
        if node_type in {"class_definition", "impl_item", "declaration_list"}:
            for child in getattr(current, "children", []):
                child_type = getattr(child, "type", "")
                if child_type in {"identifier", "type_identifier", "name"}:
                    base = _node_text(source, child).strip()
                    inner = _enclosing_def_name(current, source)
                    return f"{base}.{inner}" if inner else base
        current = getattr(current, "parent", None)
    return None


def _call_sites_from_captures(
    source: bytes,
    relative_path: str,
    captures: list[tuple[str, Any]],
) -> list[CodeCall]:
    call_nodes: list[Any] = []
    name_by_range: dict[tuple[int, int], str] = {}

    for capture_name, node in captures:
        category = _capture_category(capture_name)
        if category == "call":
            call_nodes.append(node)
        elif _is_name_capture(capture_name):
            rng = (int(getattr(node, "start_byte", 0)), int(getattr(node, "end_byte", 0)))
            name_by_range[rng] = _node_text(source, node)

    calls: list[CodeCall] = []
    seen: set[tuple[int, str]] = set()
    for call_node in call_nodes:
        cs = int(getattr(call_node, "start_byte", 0))
        ce = int(getattr(call_node, "end_byte", 0))
        callee_name: str | None = None
        for (ns, ne), ntext in name_by_range.items():
            if ns >= cs and ne <= ce:
                callee_name = ntext
                break
        if not callee_name:
            continue

        start_line, _ = _node_lines(call_node)
        caller_name = _enclosing_def_name(call_node, source) or "__module__"
        key = (start_line, callee_name)
        if key not in seen:
            seen.add(key)
            calls.append(CodeCall(
                caller_name=caller_name,
                callee_name=callee_name,
                path=relative_path,
                start_line=start_line,
            ))
    return calls


def _apply_symbol_hook(
    symbol: CapturedSymbol, context: ExtractionContext, hooks: QueryLanguageHooks
) -> CapturedSymbol | None:
    if hooks.normalize_symbol is None:
        return symbol
    return hooks.normalize_symbol(symbol, context)


def _apply_import_hook(
    item: CapturedImport, context: ExtractionContext, hooks: QueryLanguageHooks
) -> CapturedImport | None:
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

    start_line, end_line = _node_lines(node)
    exported = not name.startswith("_")
    return CapturedSymbol(
        name=name,
        kind=category,
        language=language,
        path=relative_path,
        start_line=start_line,
        end_line=end_line,
        start_byte=int(getattr(node, "start_byte", 0)),
        end_byte=int(getattr(node, "end_byte", 0)),
        signature=_capture_to_signature(category, text),
        parent=None,
        exported=exported,
        node=node,
    )


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
        if category in {"call", "name"}:
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


def _dedupe_symbols(symbols: list[CapturedSymbol]) -> list[CapturedSymbol]:
    selected: dict[tuple[int, int], CapturedSymbol] = {}
    priorities: dict[tuple[int, int], int] = {}
    for symbol in symbols:
        key = (symbol.start_byte, symbol.end_byte)
        priority = SYMBOL_KIND_PRIORITY.get(symbol.kind, 99)
        if key not in selected or priority < priorities[key]:
            selected[key] = symbol
            priorities[key] = priority
    return sorted(selected.values(), key=_symbol_key)


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


def extract_result_from_captures(
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
    call_sites = _call_sites_from_captures(source, relative_path, captures)
    return ExtractionResult(
        symbols=[_to_code_symbol(item) for item in deduped_symbols],
        imports=[_to_code_import(item) for item in deduped_imports],
        calls=call_sites,
    )


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

        return extract_result_from_captures(
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


__all__ = [
    "PythonTagQueryExtractor",
    "TagQueryExtractor",
    "extract_result_from_captures",
    "load_tags_query",
]
