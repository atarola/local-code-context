from __future__ import annotations

import re

from local_code_rag.syntax.capture_models import CapturedSymbol, ExtractionContext, QueryLanguageHooks
from local_code_rag.syntax.legacy_python import _collapse_signature, _node_text


def _nearest_ancestor(node: object, types: set[str]) -> object | None:
    current = getattr(node, "parent", None)
    while current is not None:
        if getattr(current, "type", "") in types:
            return current
        current = getattr(current, "parent", None)
    return None


def _rust_item_name(text: str) -> str | None:
    patterns = (
        r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:pub\s+)?enum\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:pub\s+)?union\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:pub\s+)?type\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:pub\s+)?trait\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)",
        r"^\s*(?:pub\s+)?macro_rules!\s+([A-Za-z_][A-Za-z0-9_]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is not None:
            return match.group(1)
    return None


def _rust_parent_from_text(text: str, node_type: str) -> str | None:
    if node_type == "trait_item":
        return _rust_item_name(text)

    header = text.split("{", 1)[0].split(" where ", 1)[0].strip()
    header = header.removeprefix("default ").strip()
    header = header.removeprefix("unsafe ").strip()
    header = header.removeprefix("pub ").strip()
    if header.startswith("impl "):
        header = header[len("impl ") :].strip()
    return header or None


def _normalize_rust_symbol(
    symbol: CapturedSymbol, context: ExtractionContext
) -> CapturedSymbol:
    node_type = getattr(symbol.node, "type", "")
    if node_type == "declaration_list" and symbol.kind in {"function", "method"}:
        return None
    if node_type in {
        "struct_item",
        "enum_item",
        "union_item",
        "type_item",
        "trait_item",
        "mod_item",
        "macro_definition",
    }:
        kind_map = {
            "struct_item": "struct",
            "enum_item": "enum",
            "union_item": "union",
            "type_item": "type",
            "trait_item": "trait",
            "mod_item": "module",
            "macro_definition": "macro",
        }
        name = _rust_item_name(_node_text(context.source, symbol.node))
        if name is None:
            return symbol
        return CapturedSymbol(
            name=name,
            kind=kind_map[node_type],
            language=symbol.language,
            path=symbol.path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            start_byte=symbol.start_byte,
            end_byte=symbol.end_byte,
            signature=symbol.signature,
            parent=None,
            exported=symbol.exported,
            node=symbol.node,
        )

    if symbol.kind not in {"function", "method"}:
        return symbol

    parent_node = _nearest_ancestor(symbol.node, {"impl_item", "trait_item"})
    if parent_node is None:
        return symbol

    parent_text = _node_text(context.source, parent_node)
    parent_type = getattr(parent_node, "type", "")
    parent = _rust_parent_from_text(parent_text, parent_type)
    if parent is None:
        return symbol

    kind = "method"
    signature = symbol.signature
    if signature is not None:
        signature = _collapse_signature(signature)

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
        parent=parent,
        exported=symbol.exported,
        node=symbol.node,
    )


RUST_HOOKS = QueryLanguageHooks(normalize_symbol=_normalize_rust_symbol)
