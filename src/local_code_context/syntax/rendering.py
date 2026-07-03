from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

from local_code_context.syntax.models import (
    CodeImport,
    CodeSymbol,
    INDEX_SCHEMA_VERSION,
    IndexRecord,
)

CHUNK_LINES = 60
CHUNK_OVERLAP = 10
MAX_SIGNATURE_CHARS = 300
MAX_SYMBOL_CHARS = 8_000


def chunk_text(text: str) -> list[tuple[int, int, str]]:
    lines = text.splitlines()
    if not lines:
        return []

    chunks: list[tuple[int, int, str]] = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    for start in range(0, len(lines), step):
        end = min(start + CHUNK_LINES, len(lines))
        chunk = "\n".join(lines[start:end]).strip()
        if chunk:
            chunks.append((start + 1, end, chunk))
        if end == len(lines):
            break
    return chunks


def make_chunk_id(
    repo: str,
    path: str,
    chunk_type: str,
    symbol: str,
    parent_symbol: str,
    part_index: int,
) -> str:
    payload = "\0".join(
        [repo, path, chunk_type, symbol, parent_symbol, str(part_index)]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_metadata(
    *,
    repo: str,
    repo_root: Path,
    relative_path: str,
    language: str,
    chunk_type: str,
    symbol: str = "",
    symbol_kind: str = "",
    parent_symbol: str = "",
    start_line: int = 0,
    end_line: int = 0,
    part_index: int = 0,
    part_count: int = 1,
) -> dict[str, Any]:
    return {
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "repo": repo,
        "repo_root": str(repo_root),
        "path": relative_path,
        "language": language,
        "chunk_type": chunk_type,
        "symbol": symbol,
        "symbol_kind": symbol_kind,
        "parent_symbol": parent_symbol,
        "start_line": start_line,
        "end_line": end_line,
        "part_index": part_index,
        "part_count": part_count,
    }


def _header_for_symbol(
    repo: str,
    relative_path: str,
    language: str,
    symbol: CodeSymbol,
    *,
    part_index: int = 0,
    part_count: int = 1,
) -> str:
    header = [
        f"Repository: {repo}",
        f"Path: {relative_path}",
        f"Language: {language}",
        f"Symbol: {symbol.name}",
        f"Kind: {symbol.kind}",
        f"Lines: {symbol.start_line}-{symbol.end_line}",
    ]
    if part_count > 1:
        header.append(f"Part: {part_index}/{part_count}")
    return "\n".join(header)


def _header_for_file_map(relative_path: str, language: str) -> str:
    return f"File: {relative_path}\nLanguage: {language}"


def _render_imports(imports: list[CodeImport]) -> str:
    if not imports:
        return "- none"
    lines: list[str] = []
    for item in imports:
        source = item.source
        if item.imported_names and not (
            len(item.imported_names) == 1 and item.imported_names[0] == item.source
        ):
            source = f"{source}: {', '.join(item.imported_names)}"
        lines.append(f"- {source}")
    return "\n".join(lines)


def _clean_signature(signature: str) -> str:
    cleaned = signature.strip()
    for prefix in ("async def ", "def ", "class "):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned


def _render_symbols(symbols: list[CodeSymbol]) -> str:
    if not symbols:
        return "- none"
    lines: list[str] = []
    for item in symbols:
        if item.kind == "class":
            summary = _clean_signature(item.signature or item.name)
            lines.append(f"- class {summary}")
            continue
        if item.kind == "method":
            summary = _clean_signature(item.signature or item.name)
            if item.parent:
                lines.append(f"- method {item.parent}.{summary}")
            else:
                lines.append(f"- method {summary}")
            continue
        if item.kind == "function":
            summary = _clean_signature(item.signature or item.name)
            lines.append(f"- function {summary}")
            continue
        if item.kind == "constant":
            summary = _clean_signature(item.signature or item.name)
            lines.append(f"- constant {summary}")
            continue
        summary = _clean_signature(item.signature or item.name)
        lines.append(f"- {item.kind} {summary}")
    return "\n".join(lines)


def build_file_map_document(
    relative_path: str,
    language: str,
    symbols: list[CodeSymbol],
    imports: list[CodeImport],
) -> str:
    return "\n".join(
        [
            _header_for_file_map(relative_path, language),
            "",
            "Imports:",
            _render_imports(imports),
            "",
            "Symbols:",
            _render_symbols(symbols),
        ]
    )


def build_file_map_record(
    *,
    repo: str,
    repo_root: Path,
    relative_path: str,
    language: str,
    symbols: list[CodeSymbol],
    imports: list[CodeImport],
) -> IndexRecord:
    max_line = max(symbol.end_line for symbol in symbols)
    return IndexRecord(
        id=make_chunk_id(repo, relative_path, "file_map", "", "", 0),
        document=build_file_map_document(relative_path, language, symbols, imports),
        metadata=_record_metadata(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            language=language,
            chunk_type="file_map",
            start_line=1,
            end_line=max_line,
            part_index=0,
            part_count=1,
        ),
    )


def build_symbol_records(
    *,
    repo: str,
    repo_root: Path,
    relative_path: str,
    language: str,
    symbol: CodeSymbol,
    source: bytes,
) -> list[IndexRecord]:
    text = source[symbol.start_byte : symbol.end_byte].decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(text) <= MAX_SYMBOL_CHARS and len(lines) <= CHUNK_LINES:
        metadata = _record_metadata(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            language=language,
            chunk_type="symbol",
            symbol=symbol.name,
            symbol_kind=symbol.kind,
            parent_symbol=symbol.parent or "",
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            part_index=0,
            part_count=1,
        )
        document = "\n".join(
            [
                _header_for_symbol(repo, relative_path, language, symbol),
                "",
                text.rstrip(),
            ]
        )
        return [
            IndexRecord(
                id=make_chunk_id(
                    repo, relative_path, "symbol", symbol.name, symbol.parent or "", 0
                ),
                document=document,
                metadata=metadata,
            )
        ]

    chunks = chunk_text(text)
    if not chunks:
        return []

    records: list[IndexRecord] = []
    total_parts = len(chunks)
    for index, (start_line, end_line, chunk) in enumerate(chunks, start=1):
        absolute_start = symbol.start_line + start_line - 1
        absolute_end = symbol.start_line + end_line - 1
        metadata = _record_metadata(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            language=language,
            chunk_type="symbol_part",
            symbol=symbol.name,
            symbol_kind=symbol.kind,
            parent_symbol=symbol.parent or "",
            start_line=absolute_start,
            end_line=absolute_end,
            part_index=index,
            part_count=total_parts,
        )
        document = "\n".join(
            [
                _header_for_symbol(
                    repo,
                    relative_path,
                    language,
                    symbol,
                    part_index=index,
                    part_count=total_parts,
                ),
                "",
                chunk.rstrip(),
            ]
        )
        records.append(
            IndexRecord(
                id=make_chunk_id(
                    repo,
                    relative_path,
                    "symbol_part",
                    symbol.name,
                    symbol.parent or "",
                    index,
                ),
                document=document,
                metadata=metadata,
            )
        )
    return records


def build_structural_records(
    *,
    repo: str,
    repo_root: Path,
    relative_path: str,
    language: str,
    source: bytes,
    symbols: list[CodeSymbol],
    imports: list[CodeImport],
) -> list[IndexRecord]:
    if not symbols:
        return []

    records: list[IndexRecord] = [
        build_file_map_record(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            language=language,
            symbols=symbols,
            imports=imports,
        )
    ]

    for symbol in symbols:
        records.extend(
            build_symbol_records(
                repo=repo,
                repo_root=repo_root,
                relative_path=relative_path,
                language=language,
                symbol=symbol,
                source=source,
            )
        )

    return records


def build_text_fallback_records(
    *,
    repo: str,
    repo_root: Path,
    relative_path: str,
    text: str,
    language: str,
    reason: str,
) -> list[IndexRecord]:
    print(
        f"using text chunks for {repo}:{relative_path}: {reason}",
        file=sys.stderr,
    )

    chunks = chunk_text(text)
    records: list[IndexRecord] = []
    total_parts = len(chunks)
    for index, (start_line, end_line, chunk) in enumerate(chunks, start=1):
        metadata = _record_metadata(
            repo=repo,
            repo_root=repo_root,
            relative_path=relative_path,
            language=language,
            chunk_type="text",
            start_line=start_line,
            end_line=end_line,
            part_index=index,
            part_count=total_parts,
        )
        record_id = make_chunk_id(repo, relative_path, "text", "", "", index)
        records.append(IndexRecord(id=record_id, document=chunk, metadata=metadata))
    return records
