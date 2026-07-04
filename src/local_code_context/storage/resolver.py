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


def _resolve_unqualified(conn: sqlite3.Connection, repo: str, path: str) -> None:
    rows = conn.execute(
        """SELECT id, callee_name FROM call_sites
           WHERE repo = ? AND path = ? AND resolution_status = 'unresolved'
             AND (callee_qualifier IS NULL OR callee_qualifier = '')""",
        (repo, path),
    ).fetchall()
    for row in rows:
        call_id = row["id"]
        callee = row["callee_name"]

        same_file = conn.execute(
            """SELECT id FROM symbols
               WHERE repo = ? AND path = ? AND name = ?""",
            (repo, path, callee),
        ).fetchall()
        if len(same_file) == 1:
            conn.execute(
                "UPDATE call_sites SET resolved_symbol_id = ?, resolution_status = 'resolved' WHERE id = ?",
                (same_file[0]["id"], call_id),
            )
            continue
        if len(same_file) > 1:
            conn.execute(
                "UPDATE call_sites SET resolution_status = 'ambiguous' WHERE id = ?",
                (call_id,),
            )
            continue

        via_imports = conn.execute(
            """SELECT ri.symbol_id
               FROM imports i
               JOIN resolved_imports ri ON ri.import_id = i.id
               WHERE i.repo = ? AND i.path = ? AND i.imported_name = ?""",
            (repo, path, callee),
        ).fetchall()
        if len(via_imports) == 1:
            conn.execute(
                "UPDATE call_sites SET resolved_symbol_id = ?, resolution_status = 'resolved' WHERE id = ?",
                (via_imports[0]["symbol_id"], call_id),
            )


def _resolve_self_method(conn: sqlite3.Connection, repo: str, path: str) -> None:
    rows = conn.execute(
        """SELECT cs.id, cs.callee_name, s.parent AS caller_parent
           FROM call_sites cs
           JOIN symbols s ON cs.caller_symbol_id = s.id
           WHERE cs.repo = ? AND cs.path = ? AND cs.resolution_status = 'unresolved'
             AND cs.callee_qualifier = 'self'""",
        (repo, path),
    ).fetchall()
    for row in rows:
        call_id = row["id"]
        callee = row["callee_name"]
        caller_parent = row["caller_parent"]
        if not caller_parent:
            continue
        syms = conn.execute(
            """SELECT id FROM symbols
               WHERE repo = ? AND path = ? AND name = ? AND kind = 'method' AND parent = ?""",
            (repo, path, callee, caller_parent),
        ).fetchall()
        if len(syms) == 1:
            conn.execute(
                "UPDATE call_sites SET resolved_symbol_id = ?, resolution_status = 'resolved' WHERE id = ?",
                (syms[0]["id"], call_id),
            )
        elif len(syms) > 1:
            conn.execute(
                "UPDATE call_sites SET resolution_status = 'ambiguous' WHERE id = ?",
                (call_id,),
            )


def _resolve_qualified(conn: sqlite3.Connection, repo: str, path: str) -> None:
    rows = conn.execute(
        """SELECT cs.id, cs.callee_name, cs.callee_qualifier
           FROM call_sites cs
           WHERE cs.repo = ? AND cs.path = ? AND cs.resolution_status = 'unresolved'
             AND cs.callee_qualifier IS NOT NULL AND cs.callee_qualifier != ''
             AND cs.callee_qualifier != 'self'""",
        (repo, path),
    ).fetchall()
    for row in rows:
        call_id = row["id"]
        callee = row["callee_name"]
        qualifier = row["callee_qualifier"]

        if qualifier.startswith("crate::"):
            _resolve_rust_crate_path(conn, repo, call_id, callee, qualifier)
        elif qualifier == "self" or qualifier == "super":
            continue
        elif qualifier.count("::") > 0:
            _resolve_rust_module_path(conn, repo, call_id, callee, qualifier)
        else:
            _resolve_python_qualified(conn, repo, path, call_id, callee, qualifier)


def _resolve_rust_crate_path(
    conn: sqlite3.Connection, repo: str, call_id: int, callee: str, qualifier: str
) -> None:
    module_part = qualifier[len("crate::"):]
    path_pattern = f"%/{module_part.replace('::', '/')}%"
    syms = conn.execute(
        """SELECT id FROM symbols
           WHERE repo = ? AND name = ? AND path LIKE ?""",
        (repo, callee, path_pattern),
    ).fetchall()
    if len(syms) == 1:
        conn.execute(
            "UPDATE call_sites SET resolved_symbol_id = ?, resolution_status = 'resolved' WHERE id = ?",
            (syms[0]["id"], call_id),
        )
    elif len(syms) > 1:
        conn.execute(
            "UPDATE call_sites SET resolution_status = 'ambiguous' WHERE id = ?",
            (call_id,),
        )


def _resolve_rust_module_path(
    conn: sqlite3.Connection, repo: str, call_id: int, callee: str, qualifier: str
) -> None:
    module_path = qualifier.replace("::", "/")
    syms = conn.execute(
        """SELECT id, path FROM symbols
           WHERE repo = ? AND name = ?""",
        (repo, qualifier),
    ).fetchall()
    if len(syms) == 1:
        target_path = syms[0]["path"]
        target_dir = str(Path(target_path).parent)
        targets = conn.execute(
            """SELECT id FROM symbols
               WHERE repo = ? AND path LIKE ? AND name = ?""",
            (repo, f"{target_dir}/%", callee),
        ).fetchall()
        if len(targets) == 1:
            conn.execute(
                "UPDATE call_sites SET resolved_symbol_id = ?, resolution_status = 'resolved' WHERE id = ?",
                (targets[0]["id"], call_id),
            )
        elif len(targets) > 1:
            conn.execute(
                "UPDATE call_sites SET resolution_status = 'ambiguous' WHERE id = ?",
                (call_id,),
            )
    else:
        targets = conn.execute(
            """SELECT id FROM symbols
               WHERE repo = ? AND path LIKE ? AND name = ?
               ORDER BY path, start_line""",
            (repo, f"%/{module_path}%", callee),
        ).fetchall()
        if len(targets) == 1:
            conn.execute(
                "UPDATE call_sites SET resolved_symbol_id = ?, resolution_status = 'resolved' WHERE id = ?",
                (targets[0]["id"], call_id),
            )
        elif len(targets) > 1:
            conn.execute(
                "UPDATE call_sites SET resolution_status = 'ambiguous' WHERE id = ?",
                (call_id,),
            )


def _resolve_python_qualified(
    conn: sqlite3.Connection, repo: str, path: str, call_id: int, callee: str, qualifier: str
) -> None:
    imported = conn.execute(
        """SELECT i.path, i.imported_name
           FROM imports i
           JOIN resolved_imports ri ON ri.import_id = i.id
           JOIN symbols s ON ri.symbol_id = s.id
           WHERE i.repo = ? AND i.path = ? AND (s.name = ? OR i.imported_name = ?)""",
        (repo, path, qualifier, qualifier),
    ).fetchall()
    if len(imported) == 1:
        target_path = imported[0]["path"]
        targets = conn.execute(
            """SELECT id FROM symbols
               WHERE repo = ? AND path = ? AND name = ?""",
            (repo, target_path, callee),
        ).fetchall()
        if len(targets) == 1:
            conn.execute(
                "UPDATE call_sites SET resolved_symbol_id = ?, resolution_status = 'resolved' WHERE id = ?",
                (targets[0]["id"], call_id),
            )
    if len(imported) > 1:
        conn.execute(
            "UPDATE call_sites SET resolution_status = 'ambiguous' WHERE id = ?",
            (call_id,),
        )


def resolve_call_sites_for_repo(db_path: Path, repo: str) -> dict[str, Any]:
    resolve_imports_for_repo(db_path, repo)

    conn = _connect(db_path)
    if conn is None:
        return {"resolved": 0, "ambiguous": 0, "unresolved": 0, "errors": []}

    try:
        conn.execute("UPDATE call_sites SET resolved_symbol_id = NULL, resolution_status = 'unresolved' WHERE repo = ?", (repo,))
        paths = conn.execute(
            "SELECT DISTINCT path FROM call_sites WHERE repo = ?", (repo,)
        ).fetchall()
        for row in paths:
            p = row["path"]
            _resolve_unqualified(conn, repo, p)
            _resolve_self_method(conn, repo, p)
            _resolve_qualified(conn, repo, p)
        conn.commit()
        counts = conn.execute(
            """SELECT
                SUM(CASE WHEN resolution_status = 'resolved' THEN 1 ELSE 0 END) AS resolved,
                SUM(CASE WHEN resolution_status = 'ambiguous' THEN 1 ELSE 0 END) AS ambiguous,
                SUM(CASE WHEN resolution_status = 'unresolved' THEN 1 ELSE 0 END) AS unresolved
               FROM call_sites WHERE repo = ?""",
            (repo,),
        ).fetchone()
        return {
            "resolved": counts["resolved"] or 0,
            "ambiguous": counts["ambiguous"] or 0,
            "unresolved": counts["unresolved"] or 0,
            "errors": [],
        }
    except sqlite3.Error as e:
        conn.rollback()
        return {"resolved": 0, "ambiguous": 0, "unresolved": 0, "errors": [str(e)]}
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
