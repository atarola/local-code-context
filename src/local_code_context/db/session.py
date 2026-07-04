from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from local_code_context.db.engine import create_engine_for_db
from local_code_context.db.schema import ensure_orm_schema


def session_factory_for(db_dir: Path) -> sessionmaker[Session]:
    engine = create_engine_for_db(db_dir)
    ensure_orm_schema(engine)
    return sessionmaker(bind=engine)


@contextmanager
def session_scope(db_dir: Path) -> Iterator[Session]:
    factory = session_factory_for(db_dir)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
