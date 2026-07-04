from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from local_code_context.db.engine import create_engine_for_db
from local_code_context.db.models import CallSite, FileVibe, ImportRecord, ResolvedImport, Symbol
from local_code_context.db.schema import ensure_orm_schema
from local_code_context.storage.schema import get_db_path
from local_code_context.syntax.models import CodeCall, CodeImport, CodeSymbol


def _language_for_path(path: str) -> str:
    suffix = Path(path).suffix.lower()
    lang_map = {
        ".py": "python",
        ".pyi": "python",
        ".rs": "rust",
        ".v": "verilog",
        ".vh": "verilog",
        ".sv": "verilog",
        ".svh": "verilog",
    }
    return lang_map.get(suffix, "")


def _extract_vibe(symbols: list[CodeSymbol]) -> str:
    parts: list[str] = []
    for sym in symbols[:5]:
        label = sym.signature or sym.name
        parts.append(label)
    return "; ".join(parts) if parts else ""


def _sym_to_orm(sym: CodeSymbol, repo: str) -> Symbol:
    return Symbol(
        repo=repo,
        path=sym.path,
        name=sym.name,
        kind=sym.kind,
        language=sym.language,
        start_line=sym.start_line,
        end_line=sym.end_line,
        parent=sym.parent or "",
        exported=1 if sym.exported else 0,
        signature=sym.signature or "",
    )


def _symbol_key(sym: CodeSymbol) -> str:
    key = sym.parent or ""
    return f"{sym.kind}:{sym.name}:{key}:{sym.start_line}"


def delete_file_xref(db_path: Path, repo: str, path: str) -> None:
    engine = create_engine_for_db(db_path)
    ensure_orm_schema(engine)
    Factory = sessionmaker(bind=engine)

    with Factory() as session, session.begin():
        session.execute(delete(CallSite).where(CallSite.repo == repo, CallSite.path == path))
        session.execute(
            delete(ResolvedImport).where(
                ResolvedImport.import_id.in_(
                    select(ImportRecord.id).where(
                        ImportRecord.repo == repo, ImportRecord.path == path
                    )
                )
            )
        )
        session.execute(delete(ImportRecord).where(ImportRecord.repo == repo, ImportRecord.path == path))
        session.execute(delete(Symbol).where(Symbol.repo == repo, Symbol.path == path))
        session.execute(delete(FileVibe).where(FileVibe.repo == repo, FileVibe.path == path))


def index_file_xref(
    db_path: Path,
    repo: str,
    path: str,
    extraction: Any | None = None,
    symbols: list[CodeSymbol] | None = None,
    imports: list[CodeImport] | None = None,
    calls: list[CodeCall] | None = None,
) -> None:
    if extraction is not None:
        syms = extraction.symbols
        imps = extraction.imports
        clls = extraction.calls
    else:
        syms = symbols or []
        imps = imports or []
        clls = calls or []

    engine = create_engine_for_db(db_path)
    ensure_orm_schema(engine)
    Factory = sessionmaker(bind=engine)

    with Factory() as session, session.begin():
        session.execute(delete(CallSite).where(CallSite.repo == repo, CallSite.path == path))
        session.execute(
            delete(ResolvedImport).where(
                ResolvedImport.import_id.in_(
                    select(ImportRecord.id).where(
                        ImportRecord.repo == repo, ImportRecord.path == path
                    )
                )
            )
        )
        session.execute(delete(ImportRecord).where(ImportRecord.repo == repo, ImportRecord.path == path))
        session.execute(delete(Symbol).where(Symbol.repo == repo, Symbol.path == path))
        session.execute(delete(FileVibe).where(FileVibe.repo == repo, FileVibe.path == path))

        symbol_id_map: dict[str, int] = {}
        for sym in syms:
            existing = session.execute(
                select(Symbol).where(
                    Symbol.repo == repo,
                    Symbol.path == sym.path,
                    Symbol.name == sym.name,
                    Symbol.kind == sym.kind,
                    Symbol.parent == (sym.parent or ""),
                    Symbol.start_line == sym.start_line,
                )
            ).scalar_one_or_none()
            if existing is not None:
                sym_id = existing.id
            else:
                session.add(_sym_to_orm(sym, repo))
                session.flush()
                sym_id = session.execute(
                    select(Symbol.id).where(
                        Symbol.repo == repo,
                        Symbol.path == sym.path,
                        Symbol.name == sym.name,
                        Symbol.kind == sym.kind,
                        Symbol.parent == (sym.parent or ""),
                        Symbol.start_line == sym.start_line,
                    )
                ).scalar_one()
            symbol_id_map[_symbol_key(sym)] = sym_id

        for imp in imps:
            names = imp.imported_names if imp.imported_names else (imp.source,)
            for name in names:
                session.add(
                    ImportRecord(
                        repo=repo,
                        path=imp.path,
                        source_module=imp.source,
                        imported_name=name,
                        start_line=imp.start_line,
                    )
                )

        language = _language_for_path(path)
        for c in clls:
            caller_sym_id = None
            if c.caller_symbol_key is not None and c.caller_symbol_key in symbol_id_map:
                caller_sym_id = symbol_id_map[c.caller_symbol_key]
            session.add(
                CallSite(
                    repo=repo,
                    path=c.path,
                    language=language,
                    caller_symbol_id=caller_sym_id,
                    callee_name=c.callee_name,
                    callee_qualifier=c.callee_qualifier,
                    start_line=c.start_line,
                    start_column=c.start_column,
                    end_line=c.end_line,
                    end_column=c.end_column,
                )
            )

        vibe = _extract_vibe(syms)
        if vibe:
            session.add(FileVibe(repo=repo, path=path, summary=vibe))
