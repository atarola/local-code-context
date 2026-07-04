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


def get_definition(
    db_path: Path,
    name: str,
    repo: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    conn = _connect(db_path)
    if conn is None:
        return []

    try:
        conditions = ["name = ?"]
        params: list[Any] = [name]
        if repo:
            conditions.append("repo = ?")
            params.append(repo)
        if path:
            conditions.append("path = ?")
            params.append(path)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)

        where = " AND ".join(conditions)
        rows = conn.execute(
            f"SELECT * FROM symbols WHERE {where} ORDER BY repo, path, start_line LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_imports(
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
            conditions.append("repo = ?")
            params.append(repo)
        if path:
            conditions.append("path = ?")
            params.append(path)

        where = " AND ".join(conditions) if conditions else "1"
        rows = conn.execute(
            f"SELECT * FROM imports WHERE {where} ORDER BY repo, path, start_line LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def trace_export(
    db_path: Path,
    name: str,
    repo: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    conn = _connect(db_path)
    if conn is None:
        return {"definition": None, "importers": []}

    try:
        conditions = ["name = ?"]
        params: list[Any] = [name]
        if repo:
            conditions.append("repo = ?")
            params.append(repo)

        where = " AND ".join(conditions)
        definitions = conn.execute(
            f"SELECT * FROM symbols WHERE {where} ORDER BY repo, path, start_line LIMIT 10",
            params,
        ).fetchall()

        importer_conditions = ["imported_name = ?"]
        importer_params: list[Any] = [name]
        if repo:
            importer_conditions.append("repo = ?")
            importer_params.append(repo)

        importer_where = " AND ".join(importer_conditions)
        importers = conn.execute(
            f"SELECT DISTINCT repo, path FROM imports WHERE {importer_where} ORDER BY repo, path LIMIT ?",
            (*importer_params, limit),
        ).fetchall()

        return {
            "definition": [dict(row) for row in definitions],
            "importers": [dict(row) for row in importers],
        }
    finally:
        conn.close()


def list_symbols(
    db_path: Path,
    repo: str | None = None,
    kind: str | None = None,
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
            conditions.append("repo = ?")
            params.append(repo)
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if path:
            conditions.append("path = ?")
            params.append(path)

        where = " AND ".join(conditions) if conditions else "1"
        rows = conn.execute(
            f"SELECT * FROM symbols WHERE {where} ORDER BY repo, path, start_line LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_file_vibe(
    db_path: Path,
    repo: str,
    path: str,
) -> str | None:
    conn = _connect(db_path)
    if conn is None:
        return None

    try:
        row = conn.execute(
            "SELECT summary FROM file_vibe WHERE repo = ? AND path = ?",
            (repo, path),
        ).fetchone()
        return row["summary"] if row else None
    finally:
        conn.close()
