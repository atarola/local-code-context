from __future__ import annotations

import sqlite3

from sqlalchemy import Connection, text
from sqlalchemy.engine import Engine

from local_code_context.db.models import Base

SCHEMA_VERSION = 2


def _get_schema_version(connection: sqlite3.Connection) -> int:
    try:
        row = connection.execute(
            "SELECT last_indexed FROM repo_meta WHERE repo = '__schema__'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None:
        return 0
    try:
        return int(row[0])
    except (ValueError, TypeError):
        return 0


def _migrate_v1_to_v2_raw(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE IF EXISTS call_sites")
    for idx in ("idx_call_sites_callee",):
        try:
            connection.execute(f"DROP INDEX IF EXISTS {idx}")
        except sqlite3.OperationalError:
            pass
    from local_code_context.storage.schema import CREATE_CALL_SITES

    connection.execute(CREATE_CALL_SITES)


def _ensure_schema_version_raw(connection: sqlite3.Connection) -> None:
    version = _get_schema_version(connection)
    if version < 2:
        connection.executescript("BEGIN TRANSACTION")
        try:
            _migrate_v1_to_v2_raw(connection)
            connection.execute(
                "INSERT OR REPLACE INTO repo_meta (id, repo, root_path, last_indexed) "
                "VALUES (-1, '__schema__', '', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def ensure_orm_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)

    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT OR IGNORE INTO repo_meta (id, repo, root_path, last_indexed) "
                "VALUES (-1, '__schema__', '', :version)"
            ),
            {"version": str(SCHEMA_VERSION)},
        )
        conn.commit()

    raw_conn = sqlite3.connect(engine.url.database)
    try:
        raw_conn.row_factory = sqlite3.Row
        _ensure_schema_version_raw(raw_conn)
    finally:
        raw_conn.close()
