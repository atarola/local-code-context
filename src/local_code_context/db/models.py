from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Symbol(Base):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="")
    language: Mapped[str] = mapped_column(String, nullable=False, default="")
    start_line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent: Mapped[str] = mapped_column(String, nullable=False, default="")
    exported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signature: Mapped[str] = mapped_column(String, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("repo", "path", "name", "kind", "parent", "start_line"),
        Index("idx_symbols_name", "repo", "name"),
        Index("idx_symbols_path", "repo", "path"),
    )


class ImportRecord(Base):
    __tablename__ = "imports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    source_module: Mapped[str] = mapped_column(String, nullable=False)
    imported_name: Mapped[str] = mapped_column(String, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_imports_source", "repo", "source_module"),
    )

    resolved_links: Mapped[list[ResolvedImport]] = relationship(
        back_populates="import_record"
    )


class CallSite(Base):
    __tablename__ = "call_sites"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    language: Mapped[str] = mapped_column(String, nullable=False, default="")
    caller_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), nullable=True
    )
    callee_name: Mapped[str] = mapped_column(String, nullable=False)
    callee_qualifier: Mapped[str | None] = mapped_column(String, nullable=True)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    start_column: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    end_column: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_symbol_id: Mapped[int | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="SET NULL"), nullable=True
    )
    resolution_status: Mapped[str] = mapped_column(
        String, nullable=False, default="unresolved"
    )

    __table_args__ = (
        Index("idx_call_sites_callee", "repo", "callee_name"),
        Index("idx_call_sites_caller", "caller_symbol_id"),
        Index("idx_call_sites_resolved", "resolved_symbol_id"),
        Index("idx_call_sites_path", "repo", "path"),
    )

    caller_symbol: Mapped[Optional[Symbol]] = relationship(
        foreign_keys=[caller_symbol_id]
    )
    resolved_symbol: Mapped[Optional[Symbol]] = relationship(
        foreign_keys=[resolved_symbol_id]
    )


class FileVibe(Base):
    __tablename__ = "file_vibe"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        UniqueConstraint("repo", "path"),
    )


class RepoMetum(Base):
    __tablename__ = "repo_meta"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    root_path: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_indexed: Mapped[str] = mapped_column(String, nullable=False, default="")


class ResolvedImport(Base):
    __tablename__ = "resolved_imports"

    import_id: Mapped[int] = mapped_column(
        ForeignKey("imports.id", ondelete="CASCADE"), primary_key=True
    )
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), primary_key=True
    )

    import_record: Mapped[ImportRecord] = relationship(back_populates="resolved_links")
    symbol: Mapped[Symbol] = relationship()
