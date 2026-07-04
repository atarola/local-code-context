from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_context.storage.schema import ensure_schema, get_db_path, open_db
from local_code_context.storage.writer import index_file_xref
from local_code_context.storage.reader import (
    get_definition,
    get_file_vibe,
    get_imports,
    list_symbols,
    trace_export,
)
from local_code_context.storage.resolver import get_resolved_imports, resolve_imports_for_repo
from local_code_context.syntax.models import CodeImport, CodeSymbol, ExtractionResult
from local_code_context.syntax.indexer import _extract_assembly_symbols


def _seed_db(db_path: Path) -> None:
    xref_db = get_db_path(db_path)
    conn = open_db(xref_db)
    ensure_schema(conn)
    conn.close()

    syms = [
        CodeSymbol(name="start", kind="function", language="rust", path="src/main.rs",
                   start_line=1, end_line=10, start_byte=0, end_byte=100,
                   signature="fn start()", parent=None, exported=True),
        CodeSymbol(name="Config", kind="struct", language="rust", path="src/config.rs",
                   start_line=5, end_line=25, start_byte=0, end_byte=200,
                   signature="struct Config", parent=None, exported=True),
        CodeSymbol(name="parse", kind="function", language="rust", path="src/parse.rs",
                   start_line=3, end_line=15, start_byte=0, end_byte=150,
                   signature="fn parse()", parent=None, exported=True),
        CodeSymbol(name="helper", kind="function", language="rust", path="src/utils.rs",
                   start_line=2, end_line=8, start_byte=0, end_byte=60,
                   signature="fn helper()", parent="start", exported=False),
        CodeSymbol(name="start", kind="function", language="python", path="src/runner.py",
                   start_line=1, end_line=5, start_byte=0, end_byte=50,
                   signature="def start()", parent=None, exported=True),
        CodeSymbol(name="init", kind="function", language="assembly", path="src/boot.s",
                   start_line=1, end_line=1, start_byte=0, end_byte=10,
                   signature="init", parent=None, exported=True),
    ]

    imps = [
        CodeImport(source="crate::config", imported_names=("Config",), path="src/main.rs", start_line=1),
        CodeImport(source="crate::parse", imported_names=("parse",), path="src/main.rs", start_line=2),
        CodeImport(source="serde::*", imported_names=(), path="src/main.rs", start_line=3),
    ]

    extraction = ExtractionResult(symbols=syms, imports=imps)
    index_file_xref(db_path=db_path, repo="test_repo", path="src/main.rs", extraction=extraction)

    extra_syms = [
        CodeSymbol(name="run", kind="function", language="rust", path="src/lib.rs",
                   start_line=10, end_line=20, start_byte=0, end_byte=100,
                   signature="fn run()", parent=None, exported=True),
    ]
    index_file_xref(db_path=db_path, repo="test_repo", path="src/lib.rs", extraction=ExtractionResult(symbols=extra_syms, imports=[]))


class StorageSchemaTests(unittest.TestCase):
    def test_schema_created_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            ensure_schema(conn)
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            conn.close()
            self.assertIn("symbols", tables)
            self.assertIn("imports", tables)
            self.assertIn("file_vibe", tables)
            self.assertIn("resolved_imports", tables)

    def test_get_db_path_appends_xref_sqlite(self) -> None:
        self.assertEqual(get_db_path(Path("/tmp/db")), Path("/tmp/db/xref.sqlite"))


class StorageWriterTests(unittest.TestCase):
    def test_index_file_xref_creates_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(name="foo", kind="function", language="python", path="a.py",
                             start_line=1, end_line=3, start_byte=0, end_byte=30,
                             signature="foo()", parent=None, exported=True)
            extraction = ExtractionResult(symbols=[sym], imports=[])
            index_file_xref(db_path=db_path, repo="r", path="a.py", extraction=extraction)
            xref_db = get_db_path(db_path)
            self.assertTrue(xref_db.exists())

    def test_index_file_xref_skips_empty_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            index_file_xref(db_path=db_path, repo="r", path="a.py", extraction=None)
            xref_db = get_db_path(db_path)
            self.assertFalse(xref_db.exists())

    def test_file_vibe_generated_from_first_five_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms = [
                CodeSymbol(name=f"sym{i}", kind="function", language="python", path="a.py",
                           start_line=i, end_line=i, start_byte=0, end_byte=1,
                           signature=f"sym{i}()", parent=None, exported=True)
                for i in range(7)
            ]
            extraction = ExtractionResult(symbols=syms, imports=[])
            index_file_xref(db_path=db_path, repo="r", path="a.py", extraction=extraction)
            vibe = get_file_vibe(db_path=db_path, repo="r", path="a.py")
            self.assertIsNotNone(vibe)
            self.assertIn("sym0()", vibe or "")
            self.assertIn("sym4()", vibe or "")
            self.assertNotIn("sym5()", vibe or "")


class StorageReaderTests(unittest.TestCase):
    def test_get_definition_finds_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = get_definition(db_path=Path(tmpdir), name="Config")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["kind"], "struct")

    def test_get_definition_filters_by_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = get_definition(db_path=Path(tmpdir), name="start", repo="test_repo")
            self.assertEqual(len(results), 2)

    def test_get_definition_filters_by_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = get_definition(db_path=Path(tmpdir), name="start", repo="test_repo", kind="function")
            self.assertEqual(len(results), 2)

    def test_get_definition_missing_db_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = get_definition(db_path=Path(tmpdir), name="anything")
            self.assertEqual(results, [])

    def test_get_definition_not_found_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = get_definition(db_path=Path(tmpdir), name="does_not_exist")
            self.assertEqual(results, [])

    def test_get_imports_returns_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = get_imports(db_path=Path(tmpdir))
            self.assertEqual(len(results), 3)

    def test_get_imports_filters_by_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = get_imports(db_path=Path(tmpdir), repo="test_repo")
            self.assertEqual(len(results), 3)

    def test_get_imports_filters_by_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = get_imports(db_path=Path(tmpdir), repo="test_repo", path="src/main.rs")
            self.assertEqual(len(results), 3)

    def test_get_imports_missing_db_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = get_imports(db_path=Path(tmpdir))
            self.assertEqual(results, [])

    def test_list_symbols_returns_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = list_symbols(db_path=Path(tmpdir))
            self.assertEqual(len(results), 7)

    def test_list_symbols_filters_by_kind(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = list_symbols(db_path=Path(tmpdir), kind="struct")
            self.assertEqual(len(results), 1)

    def test_list_symbols_filters_by_repo_and_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            results = list_symbols(db_path=Path(tmpdir), repo="test_repo", path="src/main.rs")
            self.assertEqual(len(results), 1)

    def test_list_symbols_missing_db_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = list_symbols(db_path=Path(tmpdir))
            self.assertEqual(results, [])

    def test_trace_export_shows_definition_and_importers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            result = trace_export(db_path=Path(tmpdir), name="Config")
            self.assertIsNotNone(result["definition"])
            self.assertEqual(len(result["definition"]), 1)
            self.assertEqual(result["definition"][0]["kind"], "struct")

    def test_trace_export_no_importers_for_unused_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            result = trace_export(db_path=Path(tmpdir), name="run")
            self.assertEqual(len(result["definition"]), 1)
            self.assertEqual(len(result["importers"]), 0)

    def test_trace_export_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            result = trace_export(db_path=Path(tmpdir), name="does_not_exist")
            self.assertEqual(len(result["definition"]), 0)
            self.assertEqual(result["importers"], [])

    def test_trace_export_missing_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = trace_export(db_path=Path(tmpdir), name="anything")
            self.assertIsNone(result["definition"])
            self.assertEqual(result["importers"], [])

    def test_get_file_vibe_returns_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            vibe = get_file_vibe(db_path=Path(tmpdir), repo="test_repo", path="src/main.rs")
            self.assertIsNotNone(vibe)
            self.assertIn("struct Config", vibe or "")

    def test_get_file_vibe_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            vibe = get_file_vibe(db_path=Path(tmpdir), repo="test_repo", path="does_not_exist.rs")
            self.assertIsNone(vibe)


class StorageResolverTests(unittest.TestCase):
    def test_resolver_matches_imported_name_to_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            result = resolve_imports_for_repo(db_path=Path(tmpdir), repo="test_repo")
            self.assertGreater(result["resolved"], 0)
            self.assertEqual(result["unresolved"], 1)

    def test_resolver_handles_qualified_name_last_component(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(name="Config", kind="struct", language="rust", path="src/config.rs",
                             start_line=1, end_line=1, start_byte=0, end_byte=10,
                             signature="struct Config", parent=None, exported=True)
            imp = CodeImport(source="crate::config", imported_names=("crate::config::Config",),
                             path="src/main.rs", start_line=1)
            index_file_xref(db_path=db_path, repo="r", path="src/main.rs",
                            extraction=ExtractionResult(symbols=[sym], imports=[imp]))
            result = resolve_imports_for_repo(db_path=db_path, repo="r")
            self.assertEqual(result["resolved"], 1)

    def test_resolver_missing_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_imports_for_repo(db_path=Path(tmpdir), repo="anything")
            self.assertEqual(result["resolved"], 0)
            self.assertEqual(result["unresolved"], 0)

    def test_get_resolved_imports_returns_joined_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            _seed_db(Path(tmpdir))
            resolve_imports_for_repo(db_path=Path(tmpdir), repo="test_repo")
            results = get_resolved_imports(db_path=Path(tmpdir), repo="test_repo")
            self.assertGreater(len(results), 0)
            row = results[0]
            self.assertIn("symbol_name", row)
            self.assertIn("symbol_path", row)
            self.assertIn("source_module", row)

    def test_get_resolved_imports_empty_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            results = get_resolved_imports(db_path=Path(tmpdir))
            self.assertEqual(results, [])


class AssemblyLabelExtractionTests(unittest.TestCase):
    def test_extracts_line_labels(self) -> None:
        text = "start:\n    lda #0\nloop:\n    dex\n    bne loop\n"
        result = _extract_assembly_symbols(text, "test.s")
        names = {s.name for s in result.symbols}
        self.assertIn("start", names)
        self.assertIn("loop", names)

    def test_extracts_equates(self) -> None:
        text = "FOO = $30\nBAR = $40\nstart:\n    lda FOO\n"
        result = _extract_assembly_symbols(text, "test.s")
        names = {s.name for s in result.symbols}
        self.assertIn("FOO", names)
        self.assertIn("BAR", names)
        self.assertIn("start", names)

    def test_deduplicates_labels_and_equates(self) -> None:
        text = "FOO:\nFOO = $30\n"
        result = _extract_assembly_symbols(text, "test.s")
        names = {s.name for s in result.symbols}
        self.assertIn("FOO", names)
        self.assertEqual(len(result.symbols), 1)

    def test_handles_empty_text(self) -> None:
        result = _extract_assembly_symbols("", "empty.s")
        self.assertEqual(len(result.symbols), 0)

    def test_ignores_non_label_colons(self) -> None:
        text = ".setcpu \"65c02\"\n.segment \"KERNEL\"\nmain:\n    rti\n"
        result = _extract_assembly_symbols(text, "test.s")
        names = {s.name for s in result.symbols}
        self.assertNotIn("setcpu", names)
        self.assertNotIn("segment", names)
        self.assertIn("main", names)

    def test_uses_label_kind_and_assembly_language(self) -> None:
        text = "label:\n"
        result = _extract_assembly_symbols(text, "boot.s")
        sym = result.symbols[0]
        self.assertEqual(sym.kind, "label")
        self.assertEqual(sym.language, "assembly")
        self.assertEqual(sym.path, "boot.s")

    def test_uses_constant_kind_for_equates(self) -> None:
        text = "VALUE = $ff\n"
        result = _extract_assembly_symbols(text, "const.s")
        sym = result.symbols[0]
        self.assertEqual(sym.kind, "constant")

    def test_line_labels_get_line_numbers(self) -> None:
        text = "\n\nstart:\n    nop\n"
        result = _extract_assembly_symbols(text, "test.s")
        sym = next(s for s in result.symbols if s.name == "start")
        self.assertEqual(sym.start_line, 3)

    def test_ignores_period_prefixed_directives(self) -> None:
        text = ".macro\n.endmacro\nreal_label:\n"
        result = _extract_assembly_symbols(text, "test.s")
        names = {s.name for s in result.symbols}
        self.assertNotIn("macro", names)
        self.assertNotIn("endmacro", names)
        self.assertIn("real_label", names)


if __name__ == "__main__":
    unittest.main()
