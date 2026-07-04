from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased, sessionmaker

from local_code_context.db.engine import create_engine_for_db
from local_code_context.db.models import CallSite, FileVibe, ImportRecord, ResolvedImport, Symbol
from local_code_context.db.schema import ensure_orm_schema
from local_code_context.storage.schema import get_db_path


def _orm_to_dict(obj: Any) -> dict[str, Any]:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


def _reader_session(db_dir: Path) -> Session | None:
    xref_db = get_db_path(db_dir)
    if not xref_db.exists():
        return None
    engine = create_engine_for_db(db_dir)
    ensure_orm_schema(engine)
    Factory = sessionmaker(bind=engine)
    return Factory()


def get_definition(
    db_path: Path,
    name: str,
    repo: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    session = _reader_session(db_path)
    if session is None:
        return []

    try:
        stmt = select(Symbol).where(Symbol.name == name)
        if repo:
            stmt = stmt.where(Symbol.repo == repo)
        if path:
            stmt = stmt.where(Symbol.path == path)
        if kind:
            stmt = stmt.where(Symbol.kind == kind)

        stmt = stmt.order_by(Symbol.repo, Symbol.path, Symbol.start_line).limit(limit)
        rows = session.execute(stmt).scalars().all()
        return [_orm_to_dict(r) for r in rows]
    finally:
        session.close()


def get_imports(
    db_path: Path,
    repo: str | None = None,
    path: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session = _reader_session(db_path)
    if session is None:
        return []

    try:
        stmt = select(ImportRecord)
        if repo:
            stmt = stmt.where(ImportRecord.repo == repo)
        if path:
            stmt = stmt.where(ImportRecord.path == path)

        stmt = stmt.order_by(ImportRecord.repo, ImportRecord.path, ImportRecord.start_line).limit(limit)
        rows = session.execute(stmt).scalars().all()
        return [_orm_to_dict(r) for r in rows]
    finally:
        session.close()


def trace_export(
    db_path: Path,
    name: str,
    repo: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    session = _reader_session(db_path)
    if session is None:
        return {"definition": None, "importers": []}

    try:
        stmt = select(Symbol).where(Symbol.name == name)
        if repo:
            stmt = stmt.where(Symbol.repo == repo)
        stmt = stmt.order_by(Symbol.repo, Symbol.path, Symbol.start_line).limit(10)
        definitions = session.execute(stmt).scalars().all()

        imp_stmt = select(ImportRecord).where(ImportRecord.imported_name == name)
        if repo:
            imp_stmt = imp_stmt.where(ImportRecord.repo == repo)
        imp_stmt = imp_stmt.order_by(ImportRecord.repo, ImportRecord.path).limit(limit)
        importers = session.execute(imp_stmt).scalars().all()

        return {
            "definition": [_orm_to_dict(d) for d in definitions],
            "importers": [_orm_to_dict(i) for i in importers],
        }
    finally:
        session.close()


def list_symbols(
    db_path: Path,
    repo: str | None = None,
    kind: str | None = None,
    path: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session = _reader_session(db_path)
    if session is None:
        return []

    try:
        stmt = select(Symbol)
        if repo:
            stmt = stmt.where(Symbol.repo == repo)
        if kind:
            stmt = stmt.where(Symbol.kind == kind)
        if path:
            stmt = stmt.where(Symbol.path == path)

        stmt = stmt.order_by(Symbol.repo, Symbol.path, Symbol.start_line).limit(limit)
        rows = session.execute(stmt).scalars().all()
        return [_orm_to_dict(r) for r in rows]
    finally:
        session.close()


def trace_callers(
    db_path: Path,
    callee_name: str,
    repo: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    session = _reader_session(db_path)
    if session is None:
        return []

    try:
        CallerSym = aliased(Symbol, name="caller_sym")
        ResolvedSym = aliased(Symbol, name="resolved_sym")

        stmt = select(
            CallSite,
            CallerSym.name.label("caller_sym_name"),
            CallerSym.kind.label("caller_sym_kind"),
            ResolvedSym.name.label("resolved_sym_name"),
            ResolvedSym.kind.label("resolved_sym_kind"),
            ResolvedSym.path.label("resolved_sym_path"),
        ).outerjoin(
            CallerSym, CallSite.caller_symbol_id == CallerSym.id
        ).outerjoin(
            ResolvedSym, CallSite.resolved_symbol_id == ResolvedSym.id
        ).where(
            CallSite.callee_name == callee_name
        )

        if repo:
            stmt = stmt.where(CallSite.repo == repo)

        stmt = stmt.order_by(CallSite.repo, CallSite.path, CallSite.start_line).limit(limit)

        rows = session.execute(stmt).all()
        result: list[dict[str, Any]] = []
        for row in rows:
            cs: CallSite = row[0]
            d = _orm_to_dict(cs)
            d["caller_sym_name"] = row.caller_sym_name
            d["caller_sym_kind"] = row.caller_sym_kind
            d["resolved_sym_name"] = row.resolved_sym_name
            d["resolved_sym_kind"] = row.resolved_sym_kind
            d["resolved_sym_path"] = row.resolved_sym_path
            result.append(d)
        return result
    finally:
        session.close()


def find_callers(
    db_path: Path,
    symbol_id: int,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session = _reader_session(db_path)
    if session is None:
        return []

    try:
        CallerSym = aliased(Symbol, name="caller_sym")

        stmt = select(
            CallSite,
            CallerSym.name.label("caller_sym_name"),
            CallerSym.kind.label("caller_sym_kind"),
        ).outerjoin(
            CallerSym, CallSite.caller_symbol_id == CallerSym.id
        ).where(
            CallSite.resolved_symbol_id == symbol_id
        ).order_by(
            CallSite.repo, CallSite.path, CallSite.start_line,
            CallSite.start_column, CallSite.callee_name,
        ).limit(limit)

        rows = session.execute(stmt).all()
        result: list[dict[str, Any]] = []
        for row in rows:
            cs: CallSite = row[0]
            d = _orm_to_dict(cs)
            d["caller_sym_name"] = row.caller_sym_name
            d["caller_sym_kind"] = row.caller_sym_kind
            result.append(d)
        return result
    finally:
        session.close()


def find_callees(
    db_path: Path,
    caller_symbol_id: int,
    *,
    include_unresolved: bool = True,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session = _reader_session(db_path)
    if session is None:
        return []

    try:
        ResolvedSym = aliased(Symbol, name="resolved_sym")

        stmt = select(
            CallSite,
            ResolvedSym.name.label("resolved_sym_name"),
            ResolvedSym.kind.label("resolved_sym_kind"),
            ResolvedSym.path.label("resolved_sym_path"),
        ).outerjoin(
            ResolvedSym, CallSite.resolved_symbol_id == ResolvedSym.id
        ).where(
            CallSite.caller_symbol_id == caller_symbol_id
        )

        if not include_unresolved:
            stmt = stmt.where(CallSite.resolved_symbol_id.isnot(None))

        stmt = stmt.order_by(
            CallSite.repo, CallSite.path, CallSite.start_line,
            CallSite.start_column, CallSite.callee_name,
        ).limit(limit)

        rows = session.execute(stmt).all()
        result: list[dict[str, Any]] = []
        for row in rows:
            cs: CallSite = row[0]
            d = _orm_to_dict(cs)
            d["resolved_sym_name"] = row.resolved_sym_name
            d["resolved_sym_kind"] = row.resolved_sym_kind
            d["resolved_sym_path"] = row.resolved_sym_path
            result.append(d)
        return result
    finally:
        session.close()


def find_calls_by_name(
    db_path: Path,
    repo: str,
    callee_name: str,
    *,
    path: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session = _reader_session(db_path)
    if session is None:
        return []

    try:
        CallerSym = aliased(Symbol, name="caller_sym")
        ResolvedSym = aliased(Symbol, name="resolved_sym")

        stmt = select(
            CallSite,
            CallerSym.name.label("caller_sym_name"),
            CallerSym.kind.label("caller_sym_kind"),
            ResolvedSym.name.label("resolved_sym_name"),
            ResolvedSym.kind.label("resolved_sym_kind"),
            ResolvedSym.path.label("resolved_sym_path"),
        ).outerjoin(
            CallerSym, CallSite.caller_symbol_id == CallerSym.id
        ).outerjoin(
            ResolvedSym, CallSite.resolved_symbol_id == ResolvedSym.id
        ).where(
            CallSite.repo == repo,
            CallSite.callee_name == callee_name,
        )

        if path:
            stmt = stmt.where(CallSite.path == path)

        stmt = stmt.order_by(
            CallSite.repo, CallSite.path, CallSite.start_line,
            CallSite.start_column, CallSite.callee_name,
        ).limit(limit)

        rows = session.execute(stmt).all()
        result: list[dict[str, Any]] = []
        for row in rows:
            cs: CallSite = row[0]
            d = _orm_to_dict(cs)
            d["caller_sym_name"] = row.caller_sym_name
            d["caller_sym_kind"] = row.caller_sym_kind
            d["resolved_sym_name"] = row.resolved_sym_name
            d["resolved_sym_kind"] = row.resolved_sym_kind
            d["resolved_sym_path"] = row.resolved_sym_path
            result.append(d)
        return result
    finally:
        session.close()


def get_file_vibe(
    db_path: Path,
    repo: str,
    path: str,
) -> str | None:
    session = _reader_session(db_path)
    if session is None:
        return None

    try:
        stmt = select(FileVibe).where(FileVibe.repo == repo, FileVibe.path == path)
        row = session.execute(stmt).scalar_one_or_none()
        return row.summary if row else None
    finally:
        session.close()
