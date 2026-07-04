from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from local_code_context.storage.schema import ensure_schema, get_db_path, open_db


def _connect(db_path: Path) -> sqlite3.Connection | None:
    xref_db = get_db_path(db_path)
    if not xref_db.exists():
        return None
    conn = open_db(xref_db)
    ensure_schema(conn)
    return conn


def resolve_imports_for_repo(db_path: Path, repo: str) -> dict[str, Any]:
    conn = _connect(db_path)
    if conn is None:
        return {"resolved": 0, "unresolved": 0, "errors": []}

    try:
        imports = conn.execute(
            "SELECT id, repo, path, source_module, imported_name FROM imports WHERE repo = ?",
            (repo,),
        ).fetchall()

        resolved_count = 0
        unresolved_count = 0
        errors: list[str] = []

        for imp in imports:
            name = imp["imported_name"]
            if not name or name == "*":
                unresolved_count += 1
                continue

            symbols = conn.execute(
                "SELECT id, repo, path, name, kind FROM symbols WHERE repo = ? AND name = ? ORDER BY path, start_line",
                (repo, name),
            ).fetchall()

            if not symbols:
                last = name.split("::")[-1]
                if last != name:
                    symbols = conn.execute(
                        "SELECT id, repo, path, name, kind FROM symbols WHERE repo = ? AND name = ? ORDER BY path, start_line",
                        (repo, last),
                    ).fetchall()
                if not symbols and "::" not in name and ":" not in name:
                    last = name.split(".")[-1]
                    if last != name:
                        symbols = conn.execute(
                            "SELECT id, repo, path, name, kind FROM symbols WHERE repo = ? AND name = ? ORDER BY path, start_line",
                            (repo, last),
                        ).fetchall()

            if not symbols:
                unresolved_count += 1
                continue

            for sym in symbols:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO resolved_imports (import_id, symbol_id) VALUES (?, ?)",
                        (imp["id"], sym["id"]),
                    )
                except sqlite3.Error as e:
                    errors.append(f"import {imp['id']} -> {sym['id']}: {e}")
                    continue
                resolved_count += 1

        conn.commit()
        return {
            "resolved": resolved_count,
            "unresolved": unresolved_count,
            "errors": errors,
        }
    except sqlite3.Error as e:
        conn.rollback()
        return {"resolved": 0, "unresolved": 0, "errors": [str(e)]}
    finally:
        conn.close()


def get_resolved_imports(
    db_path: Path,
    repo: str | None = None,
    path: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    if conn is None:
        return []

    try:
        conditions: list[str] = []
        params: list[Any] = []
        if repo:
            conditions.append("i.repo = ?")
            params.append(repo)
        if path:
            conditions.append("i.path = ?")
            params.append(path)

        where = " AND ".join(conditions) if conditions else "1"
        rows = conn.execute(
            f"""SELECT i.repo, i.path, i.source_module, i.imported_name,
                       s.name AS symbol_name, s.kind AS symbol_kind,
                       s.path AS symbol_path, s.start_line AS symbol_start_line
                FROM resolved_imports ri
                JOIN imports i ON ri.import_id = i.id
                JOIN symbols s ON ri.symbol_id = s.id
                WHERE {where}
                ORDER BY i.repo, i.path, i.start_line
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
