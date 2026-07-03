from __future__ import annotations

import re
from typing import Any

from local_code_rag.syntax_chunks import MAX_SIGNATURE_CHARS
from local_code_rag.syntax_models import (
    CodeImport,
    CodeSymbol,
    ExtractionResult,
    LanguageExtractor,
)


def _collapse_signature(text: str) -> str:
    signature = " ".join(text.replace("\n", " ").split())
    if len(signature) > MAX_SIGNATURE_CHARS:
        signature = signature[:MAX_SIGNATURE_CHARS].rstrip()
    return signature


def _node_text(source: bytes, node: Any) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_lines(node: Any) -> tuple[int, int]:
    return node.start_point.row + 1, node.end_point.row + 1


def _definition_name(text: str, kind: str) -> str | None:
    if kind == "class":
        match = re.search(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    else:
        match = re.search(r"^\s*(?:async\s+def|def)\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    if match is None:
        return None
    return match.group(1)


def _assignment_name(text: str) -> str | None:
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", text)
    if match is None:
        return None
    return match.group(1)


def _definition_node(node: Any) -> Any:
    if getattr(node, "type", "") != "decorated_definition":
        return node
    children = list(
        getattr(node, "named_children", []) or getattr(node, "children", []) or []
    )
    for child in children:
        if getattr(child, "type", "") in {"class_definition", "function_definition"}:
            return child
    return node


def _signature_for_node(source: bytes, node: Any) -> str | None:
    definition = _definition_node(node)
    signature = _collapse_signature(_node_text(source, definition).split(":", 1)[0])
    return signature or None


def _python_kind_for_node(node: Any, parent_kind: str | None = None) -> str | None:
    node_type = getattr(node, "type", "")
    if node_type == "class_definition":
        return "class"
    if node_type == "function_definition":
        return "method" if parent_kind == "class" else "function"
    if node_type in {"assignment", "annotated_assignment"}:
        return "constant"
    if node_type == "decorated_definition":
        return _python_kind_for_node(_definition_node(node), parent_kind=parent_kind)
    return None


def _is_useful_constant(text: str, name: str | None) -> bool:
    if name is None:
        return False
    if name == "__all__":
        return True
    return name.upper() == name


def _extract_python_import_names(text: str) -> tuple[str, ...]:
    if text.startswith("from "):
        tail = text.split(" import ", 1)[1] if " import " in text else ""
    elif text.startswith("import "):
        tail = text[len("import ") :]
    else:
        tail = text
    names: list[str] = []
    for part in tail.split(","):
        item = part.strip()
        if not item:
            continue
        if " as " in item:
            item = item.split(" as ", 1)[0].strip()
        names.append(item)
    return tuple(names)


def extract_python_imports(
    source: bytes, tree: Any, relative_path: str
) -> list[CodeImport]:
    root = getattr(tree, "root_node", None)
    if root is None:
        return []
    imports: list[CodeImport] = []
    for node in list(
        getattr(root, "named_children", []) or getattr(root, "children", []) or []
    ):
        node_type = getattr(node, "type", "")
        if node_type not in {"import_statement", "import_from_statement"}:
            continue
        text = _node_text(source, node).strip()
        if node_type == "import_from_statement":
            match = re.match(r"^from\s+(.+?)\s+import\s+(.+)$", text)
            source_name = match.group(1).strip() if match else text
            imported = _extract_python_import_names(text)
        else:
            source_name = text[len("import ") :].strip()
            imported = _extract_python_import_names(text)
        start_line, _ = _node_lines(node)
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


def _class_body(node: Any) -> Any | None:
    body = getattr(node, "child_by_field_name", None)
    if callable(body):
        result = body("body")
        if result is not None:
            return result
    for child in list(
        getattr(node, "named_children", []) or getattr(node, "children", []) or []
    ):
        if getattr(child, "type", "") == "block":
            return child
    return None


def _extract_python_constant(
    node: Any, source: bytes, relative_path: str
) -> CodeSymbol | None:
    text = _node_text(source, node)
    name = _assignment_name(text)
    if not _is_useful_constant(text, name):
        return None
    start_line, end_line = _node_lines(node)
    start_byte = int(getattr(node, "start_byte", 0))
    end_byte = int(getattr(node, "end_byte", 0))
    return CodeSymbol(
        name=name,
        kind="constant",
        language="python",
        path=relative_path,
        start_line=start_line,
        end_line=end_line,
        start_byte=start_byte,
        end_byte=end_byte,
        signature=_collapse_signature(text.split("\n", 1)[0]),
        parent=None,
        exported=not name.startswith("_"),
    )


def _extract_python_definition(
    node: Any,
    source: bytes,
    relative_path: str,
    *,
    parent: str | None = None,
    forced_kind: str | None = None,
) -> CodeSymbol | None:
    definition = _definition_node(node)
    kind = forced_kind or _python_kind_for_node(
        node, parent_kind="class" if parent else None
    )
    if kind is None:
        return None

    text = _node_text(source, definition)
    name = _definition_name(text, "class" if kind == "class" else "function")
    if name is None:
        return None

    start_line, end_line = _node_lines(node)
    start_byte = int(getattr(node, "start_byte", 0))
    end_byte = int(getattr(node, "end_byte", 0))
    signature = _signature_for_node(source, node)
    return CodeSymbol(
        name=name,
        kind=kind,
        language="python",
        path=relative_path,
        start_line=start_line,
        end_line=end_line,
        start_byte=start_byte,
        end_byte=end_byte,
        signature=signature,
        parent=parent,
        exported=not name.startswith("_"),
    )


def _extract_python_class_children(
    class_node: Any, source: bytes, relative_path: str, class_name: str
) -> list[CodeSymbol]:
    body = _class_body(_definition_node(class_node))
    if body is None:
        return []

    symbols: list[CodeSymbol] = []
    for child in list(
        getattr(body, "named_children", []) or getattr(body, "children", []) or []
    ):
        child_type = getattr(child, "type", "")
        if child_type in {"decorated_definition", "function_definition"}:
            symbol = _extract_python_definition(
                child, source, relative_path, parent=class_name, forced_kind="method"
            )
            if symbol is not None:
                symbols.append(symbol)
    return symbols


def extract_python_symbols(
    source: bytes, tree: Any, relative_path: str
) -> list[CodeSymbol]:
    root = getattr(tree, "root_node", None)
    if root is None:
        return []

    symbols: list[CodeSymbol] = []
    for node in list(
        getattr(root, "named_children", []) or getattr(root, "children", []) or []
    ):
        node_type = getattr(node, "type", "")
        if node_type == "decorated_definition":
            inner = _definition_node(node)
            inner_type = getattr(inner, "type", "")
            if inner_type == "class_definition":
                class_symbol = _extract_python_definition(
                    node, source, relative_path, forced_kind="class"
                )
                if class_symbol is not None:
                    symbols.append(class_symbol)
                    symbols.extend(
                        _extract_python_class_children(
                            node, source, relative_path, class_symbol.name
                        )
                    )
            elif inner_type == "function_definition":
                symbol = _extract_python_definition(node, source, relative_path)
                if symbol is not None:
                    symbols.append(symbol)
        elif node_type == "class_definition":
            class_symbol = _extract_python_definition(
                node, source, relative_path, forced_kind="class"
            )
            if class_symbol is not None:
                symbols.append(class_symbol)
                symbols.extend(
                    _extract_python_class_children(
                        node, source, relative_path, class_symbol.name
                    )
                )
        elif node_type == "function_definition":
            symbol = _extract_python_definition(node, source, relative_path)
            if symbol is not None:
                symbols.append(symbol)
        elif node_type in {"assignment", "annotated_assignment"}:
            symbol = _extract_python_constant(node, source, relative_path)
            if symbol is not None:
                symbols.append(symbol)

    symbols.sort(
        key=lambda item: (item.start_byte, item.end_byte, item.kind, item.name)
    )
    return symbols


class PythonSyntaxExtractor:
    language = "python"

    def extract(self, source: bytes, tree: Any, relative_path: str) -> ExtractionResult:
        return ExtractionResult(
            symbols=extract_python_symbols(source, tree, relative_path),
            imports=extract_python_imports(source, tree, relative_path),
        )
