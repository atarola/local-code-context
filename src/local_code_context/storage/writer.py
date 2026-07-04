from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from local_code_context.storage.schema import ensure_schema, get_db_path, open_db
from local_code_context.syntax.models import CodeImport, CodeSymbol


def _upsert_symbol(conn: sqlite3.Connection, symbol: CodeSymbol, repo: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO symbols
           (repo, path, name, kind, language, start_line, end_line, parent, exported, signature)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            repo,
            symbol.path,
            symbol.name,
            symbol.kind,
            symbol.language,
            symbol.start_line,
            symbol.end_line,
            symbol.parent or "",
            1 if symbol.exported else 0,
            symbol.signature or "",
        ),
    )


def _upsert_import(conn: sqlite3.Connection, imp: CodeImport, repo: str) -> None:
    names = imp.imported_names if imp.imported_names else (imp.source,)
    for name in names:
        conn.execute(
            """INSERT INTO imports
               (repo, path, source_module, imported_name, start_line)
               VALUES (?, ?, ?, ?, ?)""",
            (repo, imp.path, imp.source, name, imp.start_line),
        )


def _upsert_file_vibe(conn: sqlite3.Connection, repo: str, path: str, summary: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO file_vibe
           (repo, path, summary) VALUES (?, ?, ?)""",
        (repo, path, summary),
    )


def _extract_vibe(symbols: list[CodeSymbol]) -> str:
    parts: list[str] = []
    for sym in symbols[:5]:
        label = sym.signature or sym.name
        parts.append(label)
    return "; ".join(parts) if parts else ""


def delete_file_xref(db_path: Path, repo: str, path: str) -> None:
    xref_db = get_db_path(db_path)
    xref_db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(xref_db)
    try:
        conn.execute("DELETE FROM symbols WHERE repo = ? AND path = ?", (repo, path))
        conn.execute("DELETE FROM imports WHERE repo = ? AND path = ?", (repo, path))
        conn.execute("DELETE FROM call_sites WHERE repo = ? AND path = ?", (repo, path))
        conn.execute("DELETE FROM file_vibe WHERE repo = ? AND path = ?", (repo, path))
        conn.commit()
    finally:
        conn.close()


def index_file_xref(
    db_path: Path,
    repo: str,
    path: str,
    extraction: Any | None = None,
    symbols: list[CodeSymbol] | None = None,
    imports: list[CodeImport] | None = None,
) -> None:
    if extraction is not None:
        syms = extraction.symbols
        imps = extraction.imports
    else:
        syms = symbols or []
        imps = imports or []

    if not syms:
        return

    xref_db = get_db_path(db_path)
    xref_db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(xref_db)
    try:
        ensure_schema(conn)

        for sym in syms:
            _upsert_symbol(conn, sym, repo)

        for imp in imps:
            _upsert_import(conn, imp, repo)

        vibe = _extract_vibe(syms)
        if vibe:
            _upsert_file_vibe(conn, repo, path, vibe)

        conn.commit()
    finally:
        conn.close()
