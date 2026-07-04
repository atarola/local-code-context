from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1

CREATE_SYMBOLS = """
CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    path        TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    language    TEXT NOT NULL DEFAULT '',
    start_line  INTEGER NOT NULL DEFAULT 0,
    end_line    INTEGER NOT NULL DEFAULT 0,
    parent      TEXT NOT NULL DEFAULT '',
    exported    INTEGER NOT NULL DEFAULT 0,
    signature   TEXT NOT NULL DEFAULT '',
    UNIQUE(repo, path, name, kind, parent, start_line)
);
"""

CREATE_IMPORTS = """
CREATE TABLE IF NOT EXISTS imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL,
    path            TEXT NOT NULL,
    source_module   TEXT NOT NULL,
    imported_name   TEXT NOT NULL,
    start_line      INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_CALL_SITES = """
CREATE TABLE IF NOT EXISTS call_sites (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL,
    path            TEXT NOT NULL,
    caller_name     TEXT NOT NULL,
    callee_name     TEXT NOT NULL,
    start_line      INTEGER NOT NULL DEFAULT 0
);
"""

CREATE_FILE_VIBE = """
CREATE TABLE IF NOT EXISTS file_vibe (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL,
    path        TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    UNIQUE(repo, path)
);
"""

CREATE_REPO_META = """
CREATE TABLE IF NOT EXISTS repo_meta (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    repo        TEXT NOT NULL UNIQUE,
    root_path   TEXT NOT NULL DEFAULT '',
    last_indexed TEXT NOT NULL DEFAULT ''
);
"""

CREATE_RESOLVED_IMPORTS = """
CREATE TABLE IF NOT EXISTS resolved_imports (
    import_id   INTEGER NOT NULL,
    symbol_id   INTEGER NOT NULL,
    PRIMARY KEY (import_id, symbol_id),
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id) ON DELETE CASCADE
);
"""

CREATE_INDEX_SYMBOLS_NAME = (
    "CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(repo, name)"
)
CREATE_INDEX_SYMBOLS_PATH = (
    "CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(repo, path)"
)
CREATE_INDEX_IMPORTS_SOURCE = (
    "CREATE INDEX IF NOT EXISTS idx_imports_source ON imports(repo, source_module)"
)
CREATE_INDEX_CALL_SITES_CALLEE = (
    "CREATE INDEX IF NOT EXISTS idx_call_sites_callee ON call_sites(repo, callee_name)"
)

ALL_TABLES = [
    CREATE_SYMBOLS,
    CREATE_IMPORTS,
    CREATE_CALL_SITES,
    CREATE_FILE_VIBE,
    CREATE_REPO_META,
    CREATE_RESOLVED_IMPORTS,
]

ALL_INDEXES = [
    CREATE_INDEX_SYMBOLS_NAME,
    CREATE_INDEX_SYMBOLS_PATH,
    CREATE_INDEX_IMPORTS_SOURCE,
    CREATE_INDEX_CALL_SITES_CALLEE,
]


def get_db_path(db_dir: Path) -> Path:
    return db_dir / "xref.sqlite"


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    for stmt in ALL_TABLES:
        conn.execute(stmt)
    for stmt in ALL_INDEXES:
        conn.execute(stmt)
    conn.execute(
        "INSERT OR IGNORE INTO repo_meta (id, repo, root_path, last_indexed) "
        "VALUES (-1, '__schema__', '', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
