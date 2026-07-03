from __future__ import annotations

from local_code_context.syntax.capture_models import (
    CapturedImport,
    CapturedSymbol,
    ExtractionContext,
    QueryLanguageHooks,
)
from local_code_context.syntax.legacy_python import (
    _assignment_name,
    _collapse_signature,
    _definition_name,
    _node_lines,
    _node_text,
    _is_useful_constant,
)


def _nearest_ancestor(node: object, types: set[str]) -> object | None:
    current = getattr(node, "parent", None)
    while current is not None:
        if getattr(current, "type", "") in types:
            return current
        current = getattr(current, "parent", None)
    return None


def _normalize_python_symbol(
    symbol: CapturedSymbol, context: ExtractionContext
) -> CapturedSymbol:
    class_node = _nearest_ancestor(context.node, {"class_definition"})
    if class_node is None:
        signature = symbol.signature
        if signature is not None and symbol.kind in {"class", "function", "method"}:
            signature = _collapse_signature(signature.split(":", 1)[0])
        if signature == symbol.signature:
            return symbol
        return CapturedSymbol(
            name=symbol.name,
            kind=symbol.kind,
            language=symbol.language,
            path=symbol.path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            start_byte=symbol.start_byte,
            end_byte=symbol.end_byte,
            signature=signature,
            parent=symbol.parent,
            exported=symbol.exported,
            node=symbol.node,
        )
    class_name = _definition_name(_node_text(context.source, class_node), "class")
    if class_name is None:
        return symbol
    kind = symbol.kind
    if kind == "function":
        kind = "method"
    if kind != "method":
        signature = symbol.signature
        if signature is not None and symbol.kind in {"class", "function", "method"}:
            signature = _collapse_signature(signature.split(":", 1)[0])
        if signature == symbol.signature and kind == symbol.kind:
            return symbol
        return CapturedSymbol(
            name=symbol.name,
            kind=kind,
            language=symbol.language,
            path=symbol.path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            start_byte=symbol.start_byte,
            end_byte=symbol.end_byte,
            signature=signature,
            parent=symbol.parent,
            exported=symbol.exported,
            node=symbol.node,
        )
    signature = symbol.signature
    if signature is not None:
        signature = _collapse_signature(signature.split(":", 1)[0])
    if signature == symbol.signature and kind == symbol.kind and class_name == symbol.parent:
        return symbol
    return CapturedSymbol(
        name=symbol.name,
        kind=kind,
        language=symbol.language,
        path=symbol.path,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
        start_byte=symbol.start_byte,
        end_byte=symbol.end_byte,
        signature=signature,
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
        if node_type != "assignment":
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


PYTHON_HOOKS = QueryLanguageHooks(
    normalize_symbol=_normalize_python_symbol,
    postprocess=_augment_python_query_result,
)
