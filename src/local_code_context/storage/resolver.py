from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from local_code_context.db.engine import create_engine_for_db
from local_code_context.db.models import CallSite, ImportRecord, ResolvedImport, Symbol
from local_code_context.db.schema import ensure_orm_schema
from local_code_context.storage.schema import get_db_path


def _resolver_session(db_dir: Path) -> Session | None:
    xref_db = get_db_path(db_dir)
    if not xref_db.exists():
        return None
    engine = create_engine_for_db(db_dir)
    ensure_orm_schema(engine)
    Factory = sessionmaker(bind=engine)
    return Factory()


def resolve_imports_for_repo(db_path: Path, repo: str) -> dict[str, Any]:
    session = _resolver_session(db_path)
    if session is None:
        return {"resolved": 0, "unresolved": 0, "errors": []}

    try:
        session.execute(
            delete(ResolvedImport).where(
                ResolvedImport.import_id.in_(
                    select(ImportRecord.id).where(ImportRecord.repo == repo)
                )
            )
        )

        imports = session.execute(
            select(ImportRecord).where(ImportRecord.repo == repo)
        ).scalars().all()

        resolved_count = 0
        unresolved_count = 0
        errors: list[str] = []

        for imp in imports:
            name = imp.imported_name
            if not name or name == "*":
                unresolved_count += 1
                continue

            symbols = session.execute(
                select(Symbol).where(Symbol.repo == repo, Symbol.name == name)
                .order_by(Symbol.path, Symbol.start_line)
            ).scalars().all()

            if not symbols:
                last = name.split("::")[-1]
                if last != name:
                    symbols = session.execute(
                        select(Symbol).where(Symbol.repo == repo, Symbol.name == last)
                        .order_by(Symbol.path, Symbol.start_line)
                    ).scalars().all()
                if not symbols and "::" not in name and ":" not in name:
                    last = name.split(".")[-1]
                    if last != name:
                        symbols = session.execute(
                            select(Symbol).where(Symbol.repo == repo, Symbol.name == last)
                            .order_by(Symbol.path, Symbol.start_line)
                        ).scalars().all()

            if not symbols:
                unresolved_count += 1
                continue

            for sym in symbols:
                try:
                    session.add(ResolvedImport(import_id=imp.id, symbol_id=sym.id))
                    session.flush()
                except Exception as e:
                    errors.append(f"import {imp.id} -> {sym.id}: {e}")
                    continue
                resolved_count += 1

        session.commit()
        return {
            "resolved": resolved_count,
            "unresolved": unresolved_count,
            "errors": errors,
        }
    except Exception as e:
        session.rollback()
        return {"resolved": 0, "unresolved": 0, "errors": [str(e)]}
    finally:
        session.close()


def _resolve_unqualified(session: Session, repo: str, path: str) -> None:
    rows = session.execute(
        select(CallSite).where(
            CallSite.repo == repo,
            CallSite.path == path,
            CallSite.resolution_status == "unresolved",
            (CallSite.callee_qualifier.is_(None)) | (CallSite.callee_qualifier == ""),
        )
    ).scalars().all()

    for cs in rows:
        call_id = cs.id
        callee = cs.callee_name

        same_file = session.execute(
            select(Symbol).where(
                Symbol.repo == repo, Symbol.path == path, Symbol.name == callee
            )
        ).scalars().all()

        if len(same_file) == 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolved_symbol_id=same_file[0].id,
                    resolution_status="resolved",
                )
            )
            continue
        if len(same_file) > 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolution_status="ambiguous",
                )
            )
            continue

        via_imports = session.execute(
            select(ResolvedImport.symbol_id).where(
                ResolvedImport.import_id.in_(
                    select(ImportRecord.id).where(
                        ImportRecord.repo == repo,
                        ImportRecord.path == path,
                        ImportRecord.imported_name == callee,
                    )
                )
            )
        ).scalars().all()

        if len(via_imports) == 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolved_symbol_id=via_imports[0],
                    resolution_status="resolved",
                )
            )


def _resolve_self_method(session: Session, repo: str, path: str) -> None:
    rows = session.execute(
        select(CallSite).join(
            Symbol, CallSite.caller_symbol_id == Symbol.id
        ).where(
            CallSite.repo == repo,
            CallSite.path == path,
            CallSite.resolution_status == "unresolved",
            CallSite.callee_qualifier == "self",
        )
    ).scalars().all()

    for cs in rows:
        call_id = cs.id
        callee = cs.callee_name

        caller = session.get(Symbol, cs.caller_symbol_id)
        if caller is None or not caller.parent:
            continue

        syms = session.execute(
            select(Symbol).where(
                Symbol.repo == repo,
                Symbol.path == path,
                Symbol.name == callee,
                Symbol.kind == "method",
                Symbol.parent == caller.parent,
            )
        ).scalars().all()

        if len(syms) == 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolved_symbol_id=syms[0].id,
                    resolution_status="resolved",
                )
            )
        elif len(syms) > 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolution_status="ambiguous",
                )
            )


def _resolve_qualified(session: Session, repo: str, path: str) -> None:
    rows = session.execute(
        select(CallSite).where(
            CallSite.repo == repo,
            CallSite.path == path,
            CallSite.resolution_status == "unresolved",
            CallSite.callee_qualifier.isnot(None),
            CallSite.callee_qualifier != "",
            CallSite.callee_qualifier != "self",
        )
    ).scalars().all()

    for cs in rows:
        call_id = cs.id
        callee = cs.callee_name
        qualifier = cs.callee_qualifier

        if qualifier.startswith("crate::"):
            _resolve_rust_crate_path(session, repo, call_id, callee, qualifier)
        elif qualifier == "self" or qualifier == "super":
            continue
        elif qualifier.count("::") > 0:
            _resolve_rust_module_path(session, repo, call_id, callee, qualifier)
        else:
            _resolve_python_qualified(session, repo, path, call_id, callee, qualifier)


def _resolve_rust_crate_path(
    session: Session, repo: str, call_id: int, callee: str, qualifier: str
) -> None:
    module_part = qualifier[len("crate::"):]
    path_pattern = f"%/{module_part.replace('::', '/')}%"
    syms = session.execute(
        select(Symbol).where(
            Symbol.repo == repo,
            Symbol.name == callee,
            Symbol.path.like(path_pattern),
        )
    ).scalars().all()

    if len(syms) == 1:
        session.execute(
            update(CallSite).where(CallSite.id == call_id).values(
                resolved_symbol_id=syms[0].id,
                resolution_status="resolved",
            )
        )
    elif len(syms) > 1:
        session.execute(
            update(CallSite).where(CallSite.id == call_id).values(
                resolution_status="ambiguous",
            )
        )


def _resolve_rust_module_path(
    session: Session, repo: str, call_id: int, callee: str, qualifier: str
) -> None:
    module_path = qualifier.replace("::", "/")

    syms = session.execute(
        select(Symbol).where(
            Symbol.repo == repo,
            Symbol.name == qualifier,
        )
    ).scalars().all()

    if len(syms) == 1:
        target_path = syms[0].path
        target_dir = str(Path(target_path).parent)
        targets = session.execute(
            select(Symbol).where(
                Symbol.repo == repo,
                Symbol.path.like(f"{target_dir}/%"),
                Symbol.name == callee,
            )
        ).scalars().all()

        if len(targets) == 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolved_symbol_id=targets[0].id,
                    resolution_status="resolved",
                )
            )
        elif len(targets) > 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolution_status="ambiguous",
                )
            )
    else:
        targets = session.execute(
            select(Symbol).where(
                Symbol.repo == repo,
                Symbol.path.like(f"%/{module_path}%"),
                Symbol.name == callee,
            ).order_by(Symbol.path, Symbol.start_line)
        ).scalars().all()

        if len(targets) == 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolved_symbol_id=targets[0].id,
                    resolution_status="resolved",
                )
            )
        elif len(targets) > 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolution_status="ambiguous",
                )
            )


def _resolve_python_qualified(
    session: Session, repo: str, path: str, call_id: int, callee: str, qualifier: str
) -> None:
    imported = session.execute(
        select(ImportRecord).join(
            ResolvedImport, ResolvedImport.import_id == ImportRecord.id
        ).join(
            Symbol, ResolvedImport.symbol_id == Symbol.id
        ).where(
            ImportRecord.repo == repo,
            ImportRecord.path == path,
            (Symbol.name == qualifier) | (ImportRecord.imported_name == qualifier),
        )
    ).scalars().all()

    if len(imported) == 1:
        target_path = imported[0].path
        targets = session.execute(
            select(Symbol).where(
                Symbol.repo == repo,
                Symbol.path == target_path,
                Symbol.name == callee,
            )
        ).scalars().all()

        if len(targets) == 1:
            session.execute(
                update(CallSite).where(CallSite.id == call_id).values(
                    resolved_symbol_id=targets[0].id,
                    resolution_status="resolved",
                )
            )
    if len(imported) > 1:
        session.execute(
            update(CallSite).where(CallSite.id == call_id).values(
                resolution_status="ambiguous",
            )
        )


def resolve_call_sites_for_repo(db_path: Path, repo: str) -> dict[str, Any]:
    resolve_imports_for_repo(db_path, repo)

    session = _resolver_session(db_path)
    if session is None:
        return {"resolved": 0, "ambiguous": 0, "unresolved": 0, "errors": []}

    try:
        session.execute(
            update(CallSite).where(CallSite.repo == repo).values(
                resolved_symbol_id=None,
                resolution_status="unresolved",
            )
        )

        paths = session.execute(
            select(CallSite.path.distinct()).where(CallSite.repo == repo)
        ).scalars().all()

        for p in paths:
            _resolve_unqualified(session, repo, p)
            _resolve_self_method(session, repo, p)
            _resolve_qualified(session, repo, p)

        session.commit()

        from sqlalchemy import func

        resolved = session.execute(
            select(func.count(CallSite.id)).where(
                CallSite.repo == repo, CallSite.resolution_status == "resolved"
            )
        ).scalar() or 0
        ambiguous = session.execute(
            select(func.count(CallSite.id)).where(
                CallSite.repo == repo, CallSite.resolution_status == "ambiguous"
            )
        ).scalar() or 0
        unresolved = session.execute(
            select(func.count(CallSite.id)).where(
                CallSite.repo == repo, CallSite.resolution_status == "unresolved"
            )
        ).scalar() or 0

        return {
            "resolved": resolved,
            "ambiguous": ambiguous,
            "unresolved": unresolved,
            "errors": [],
        }
    except Exception as e:
        session.rollback()
        return {"resolved": 0, "ambiguous": 0, "unresolved": 0, "errors": [str(e)]}
    finally:
        session.close()


def resolve_repo_relationships(db_path: Path, repo: str) -> dict[str, Any]:
    import_stats = resolve_imports_for_repo(db_path, repo)
    call_stats = resolve_call_sites_for_repo(db_path, repo)
    return call_stats


def get_resolved_imports(
    db_path: Path,
    repo: str | None = None,
    path: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    session = _resolver_session(db_path)
    if session is None:
        return []

    try:
        stmt = select(
            ImportRecord.repo,
            ImportRecord.path,
            ImportRecord.source_module,
            ImportRecord.imported_name,
            Symbol.name.label("symbol_name"),
            Symbol.kind.label("symbol_kind"),
            Symbol.path.label("symbol_path"),
            Symbol.start_line.label("symbol_start_line"),
        ).select_from(ResolvedImport).join(
            ImportRecord, ResolvedImport.import_id == ImportRecord.id
        ).join(
            Symbol, ResolvedImport.symbol_id == Symbol.id
        )

        if repo:
            stmt = stmt.where(ImportRecord.repo == repo)
        if path:
            stmt = stmt.where(ImportRecord.path == path)

        stmt = stmt.order_by(ImportRecord.repo, ImportRecord.path, ImportRecord.start_line).limit(limit)

        rows = session.execute(stmt).mappings().all()
        return [dict(row) for row in rows]
    finally:
        session.close()
