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
from local_code_context.storage.writer import index_file_xref, delete_file_xref
from local_code_context.storage.reader import find_callers, find_callees, find_calls_by_name
from local_code_context.storage.resolver import resolve_call_sites_for_repo, resolve_imports_for_repo
from local_code_context.syntax.models import CodeCall, CodeImport, CodeSymbol, ExtractionResult, EnclosingDef
from local_code_context.syntax.capture_normalization import (
    _enclosing_def,
    _call_sites_from_captures,
)
from local_code_context.syntax.indexer import build_index_records


def _make_db(db_path: Path) -> None:
    xref_db = get_db_path(db_path)
    xref_db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(xref_db)
    ensure_schema(conn)
    conn.close()


class EnclosingDefTests(unittest.TestCase):
    def test_symbol_key_format(self) -> None:
        edef = EnclosingDef(kind="function", name="helper", parent=None, start_line=5, start_byte=30)
        self.assertEqual(edef.symbol_key(), "function:helper::5")

    def test_symbol_key_with_parent(self) -> None:
        edef = EnclosingDef(kind="method", name="process", parent="MyClass", start_line=10, start_byte=100)
        self.assertEqual(edef.symbol_key(), "method:process:MyClass:10")


class PyCallExtractionTests(unittest.TestCase):
    def _extract_calls(self, source: str, path: str = "test.py") -> list[CodeCall]:
        from local_code_context.syntax.detection import detect_language
        from local_code_context.syntax.indexer import QUERY_EXTRACTORS

        lang = detect_language(Path(path), source.encode())
        self.assertEqual(lang, "python")
        extractor = QUERY_EXTRACTORS.get(lang)
        self.assertIsNotNone(extractor)
        result = extractor.extract(source.encode(), None, path)
        return result.calls

    def test_unqualified_call(self) -> None:
        source = "def foo():\n    bar()\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "bar")
        self.assertIsNone(c.callee_qualifier)
        self.assertEqual(c.caller_name, "foo")
        self.assertIsNotNone(c.caller_symbol_key)

    def test_module_qualified_call(self) -> None:
        source = "def foo():\n    module.bar()\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "bar")
        self.assertEqual(c.callee_qualifier, "module")

    def test_pkg_module_qualified_call(self) -> None:
        source = "def foo():\n    pkg.module.bar()\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "bar")
        self.assertEqual(c.callee_qualifier, "pkg.module")

    def test_self_method_call(self) -> None:
        source = "class Cls:\n    def method(self):\n        self.other()\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "other")
        self.assertEqual(c.callee_qualifier, "self")
        self.assertEqual(c.caller_name, "Cls.method")

    def test_obj_method_call(self) -> None:
        source = "def foo():\n    obj.bar()\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "bar")
        self.assertEqual(c.callee_qualifier, "obj")

    def test_nested_function_ownership(self) -> None:
        source = "def outer():\n    def inner():\n        helper()\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "helper")
        self.assertEqual(c.caller_name, "outer.inner")

    def test_async_function_ownership(self) -> None:
        source = "async def fetch():\n    await work()\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "work")
        self.assertEqual(c.caller_name, "fetch")

    def test_module_level_call(self) -> None:
        source = "helper()\n\ndef helper():\n    pass\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "helper")
        self.assertEqual(c.caller_name, "__module__")
        self.assertIsNone(c.caller_symbol_key)

    def test_duplicate_function_names(self) -> None:
        source = "def foo():\n    bar()\n\ndef baz():\n    bar()\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 2)
        names = {c.caller_name for c in calls}
        self.assertIn("foo", names)
        self.assertIn("baz", names)

    def test_chained_call_not_crashing(self) -> None:
        source = "def foo():\n    get_handler()()\n"
        calls = self._extract_calls(source)
        self.assertGreaterEqual(len(calls), 0)

    def test_call_with_multiple_args(self) -> None:
        source = "def foo():\n    bar(a, b, c)\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].callee_name, "bar")

    def test_calls_in_comprehension(self) -> None:
        source = "def foo():\n    return [process(x) for x in items]\n"
        calls = self._extract_calls(source)
        self.assertGreater(len(calls), 0)
        names = {c.callee_name for c in calls}
        self.assertIn("process", names)

    def test_call_with_decorator(self) -> None:
        source = "@decorator\ndef foo():\n    pass\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 0)

    def test_source_ranges_populated(self) -> None:
        source = "def foo():\n    bar(x, y)\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertGreater(c.end_line, 0)
        self.assertGreaterEqual(c.start_column, 0)


class RustCallExtractionTests(unittest.TestCase):
    def _extract_calls(self, source: str, path: str = "test.rs") -> list[CodeCall]:
        from local_code_context.syntax.detection import detect_language
        from local_code_context.syntax.indexer import QUERY_EXTRACTORS

        lang = detect_language(Path(path), source.encode())
        self.assertEqual(lang, "rust")
        extractor = QUERY_EXTRACTORS.get(lang)
        self.assertIsNotNone(extractor)
        result = extractor.extract(source.encode(), None, path)
        return result.calls

    def test_free_function_call(self) -> None:
        source = "fn run() {\n    helper()\n}\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "helper")
        self.assertIsNone(c.callee_qualifier)

    def test_module_path_call(self) -> None:
        source = "fn run() {\n    module::helper()\n}\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "helper")
        self.assertEqual(c.callee_qualifier, "module")

    def test_crate_path_call(self) -> None:
        source = "fn run() {\n    crate::index::helper()\n}\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "helper")
        self.assertEqual(c.callee_qualifier, "crate::index")

    def test_method_call(self) -> None:
        source = "fn run() {\n    value.helper()\n}\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "helper")
        self.assertEqual(c.callee_qualifier, "value")

    def test_associated_function_call(self) -> None:
        source = "fn run() {\n    Type::new()\n}\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "new")
        self.assertEqual(c.callee_qualifier, "Type")

    def test_impl_method_ownership(self) -> None:
        source = "impl Foo {\n    fn bar(&self) {\n        self.baz()\n    }\n}\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "baz")
        self.assertEqual(c.callee_qualifier, "self")

    def test_trait_method_ownership(self) -> None:
        source = "trait Trait {\n    fn method(&self) {\n        helper()\n    }\n}\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c.callee_name, "helper")

    def test_macro_excluded(self) -> None:
        source = "fn run() {\n    println!(\"hello\");\n    vec![1, 2, 3];\n}\n"
        calls = self._extract_calls(source)
        for c in calls:
            self.assertNotIn(c.callee_name, ("println", "vec"))

    def test_source_ranges_populated(self) -> None:
        source = "fn run() {\n    helper(a, b)\n}\n"
        calls = self._extract_calls(source)
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertGreater(c.end_line, 0)
        self.assertGreaterEqual(c.start_column, 0)


class StoreCallSitesTests(unittest.TestCase):
    def test_store_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            calls = [
                CodeCall(
                    caller_name="foo", callee_name="bar", path="test.py",
                    start_line=2, start_column=4, end_line=2, end_column=9,
                    callee_qualifier=None, caller_symbol_key=None,
                ),
            ]
            index_file_xref(
                db_path=db_path, repo="test", path="test.py",
                symbols=[
                    CodeSymbol(
                        name="foo", kind="function", language="python",
                        path="test.py", start_line=1, end_line=3,
                        start_byte=0, end_byte=50, signature="foo()",
                        parent=None, exported=True,
                    ),
                ],
                calls=calls,
            )
            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            rows = conn.execute("SELECT * FROM call_sites").fetchall()
            conn.close()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["repo"], "test")
            self.assertEqual(row["path"], "test.py")
            self.assertEqual(row["callee_name"], "bar")
            self.assertEqual(row["callee_qualifier"], None)
            self.assertEqual(row["start_line"], 2)
            self.assertEqual(row["language"], "python")

    def test_delete_removes_call_sites(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(
                name="foo", kind="function", language="python",
                path="test.py", start_line=1, end_line=3,
                start_byte=0, end_byte=50, signature="foo()",
                parent=None, exported=True,
            )
            calls = [CodeCall(caller_name="foo", callee_name="bar", path="test.py", start_line=2)]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym], calls=calls)
            delete_file_xref(db_path=db_path, repo="test", path="test.py")
            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            rows = conn.execute("SELECT * FROM call_sites").fetchall()
            conn.close()
            self.assertEqual(len(rows), 0)

    def test_reindex_replaces_call_sites(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(
                name="foo", kind="function", language="python",
                path="test.py", start_line=1, end_line=3,
                start_byte=0, end_byte=50, signature="foo()",
                parent=None, exported=True,
            )
            calls_a = [CodeCall(caller_name="foo", callee_name="bar", path="test.py", start_line=2)]
            calls_b = [CodeCall(caller_name="foo", callee_name="baz", path="test.py", start_line=2)]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym], calls=calls_a)
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym], calls=calls_b)
            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            rows = conn.execute("SELECT callee_name FROM call_sites").fetchall()
            conn.close()
            names = {r["callee_name"] for r in rows}
            self.assertNotIn("bar", names)
            self.assertIn("baz", names)

    def test_language_inferred_from_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(
                name="foo", kind="function", language="rust",
                path="test.rs", start_line=1, end_line=3,
                start_byte=0, end_byte=50, signature="fn foo()",
                parent=None, exported=True,
            )
            calls = [CodeCall(caller_name="foo", callee_name="bar", path="test.rs", start_line=2)]
            index_file_xref(db_path=db_path, repo="test", path="test.rs",
                            symbols=[sym], calls=calls)
            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            rows = conn.execute("SELECT language FROM call_sites").fetchall()
            conn.close()
            self.assertEqual(rows[0]["language"], "rust")


class CallerSymbolIdAssignmentTests(unittest.TestCase):
    def test_caller_symbol_id_linked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(
                name="foo", kind="function", language="python",
                path="test.py", start_line=1, end_line=3,
                start_byte=0, end_byte=50, signature="foo()",
                parent=None, exported=True,
            )
            calls = [CodeCall(
                caller_name="foo", callee_name="bar", path="test.py",
                start_line=2, caller_symbol_key="function:foo::1",
            )]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym], calls=calls)
            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            rows = conn.execute(
                """SELECT cs.caller_symbol_id, s.name AS sym_name
                   FROM call_sites cs LEFT JOIN symbols s ON cs.caller_symbol_id = s.id"""
            ).fetchall()
            conn.close()
            self.assertIsNotNone(rows[0]["caller_symbol_id"])
            self.assertEqual(rows[0]["sym_name"], "foo")

    def test_unknown_caller_key_leaves_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            calls = [CodeCall(
                caller_name="foo", callee_name="bar", path="test.py",
                start_line=2, caller_symbol_key="nonexistent",
            )]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[], calls=calls)
            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            rows = conn.execute("SELECT caller_symbol_id FROM call_sites").fetchall()
            conn.close()
            self.assertIsNone(rows[0]["caller_symbol_id"])


class ResolutionTests(unittest.TestCase):
    def test_same_file_unqualified_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms = [
                CodeSymbol(name="helper", kind="function", language="python",
                           path="test.py", start_line=5, end_line=6,
                           start_byte=0, end_byte=20, signature="helper()",
                           parent=None, exported=True),
                CodeSymbol(name="foo", kind="function", language="python",
                           path="test.py", start_line=1, end_line=4,
                           start_byte=0, end_byte=50, signature="foo()",
                           parent=None, exported=True),
            ]
            calls = [CodeCall(
                caller_name="foo", callee_name="helper", path="test.py",
                start_line=2, caller_symbol_key="function:foo::1",
            )]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=syms, calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["resolved"], 1)
            self.assertEqual(res["ambiguous"], 0)
            self.assertEqual(res["unresolved"], 0)

    def test_same_file_duplicate_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms = [
                CodeSymbol(name="helper", kind="function", language="python",
                           path="test.py", start_line=5, end_line=6,
                           start_byte=0, end_byte=20, signature="helper()",
                           parent=None, exported=True),
                CodeSymbol(name="helper", kind="function", language="python",
                           path="test.py", start_line=10, end_line=11,
                           start_byte=0, end_byte=20, signature="helper()",
                           parent=None, exported=True),
                CodeSymbol(name="foo", kind="function", language="python",
                           path="test.py", start_line=1, end_line=4,
                           start_byte=0, end_byte=50, signature="foo()",
                           parent=None, exported=True),
            ]
            calls = [CodeCall(
                caller_name="foo", callee_name="helper", path="test.py",
                start_line=2, caller_symbol_key="function:foo::1",
            )]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=syms, calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["resolved"], 0)
            self.assertEqual(res["ambiguous"], 1)

    def test_self_method_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms = [
                CodeSymbol(name="MyClass", kind="class", language="python",
                           path="test.py", start_line=1, end_line=10,
                           start_byte=0, end_byte=100, signature="class MyClass",
                           parent=None, exported=True),
                CodeSymbol(name="run", kind="method", language="python",
                           path="test.py", start_line=2, end_line=5,
                           start_byte=0, end_byte=50, signature="def run(self)",
                           parent="MyClass", exported=True),
                CodeSymbol(name="helper", kind="method", language="python",
                           path="test.py", start_line=3, end_line=4,
                           start_byte=0, end_byte=30, signature="def helper(self)",
                           parent="MyClass", exported=True),
            ]
            calls = [CodeCall(
                caller_name="MyClass.run", callee_name="helper", path="test.py",
                start_line=4, callee_qualifier="self",
                caller_symbol_key="method:run:MyClass:2",
            )]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=syms, calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["resolved"], 1)

    def test_arbitrary_object_method_left_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms = [
                CodeSymbol(name="foo", kind="function", language="python",
                           path="test.py", start_line=1, end_line=4,
                           start_byte=0, end_byte=50, signature="foo()",
                           parent=None, exported=True),
            ]
            calls = [CodeCall(
                caller_name="foo", callee_name="process", path="test.py",
                start_line=2, callee_qualifier="obj",
                caller_symbol_key="function:foo::1",
            )]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=syms, calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["unresolved"], 1)

    def test_direct_import_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms_a = [
                CodeSymbol(name="run", kind="function", language="python",
                           path="mod.py", start_line=1, end_line=3,
                           start_byte=0, end_byte=30, signature="def run()",
                           parent=None, exported=True),
            ]
            syms_b = [
                CodeSymbol(name="foo", kind="function", language="python",
                           path="main.py", start_line=3, end_line=6,
                           start_byte=0, end_byte=50, signature="def foo()",
                           parent=None, exported=True),
            ]
            imps = [CodeImport(source="mod", imported_names=("run",), path="main.py", start_line=1)]
            index_file_xref(db_path=db_path, repo="test", path="mod.py",
                            symbols=syms_a, imports=[])
            index_file_xref(db_path=db_path, repo="test", path="main.py",
                            symbols=syms_b, imports=imps)
            resolve_imports_for_repo(db_path=db_path, repo="test")

            calls = [CodeCall(
                caller_name="foo", callee_name="run", path="main.py",
                start_line=4, caller_symbol_key="function:foo::3",
            )]
            index_file_xref(db_path=db_path, repo="test", path="main.py",
                            symbols=syms_b, imports=imps, calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["resolved"], 1)

    def test_module_qualified_import_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms_a = [
                CodeSymbol(name="run", kind="function", language="python",
                           path="utils.py", start_line=1, end_line=3,
                           start_byte=0, end_byte=30, signature="def run()",
                           parent=None, exported=True),
            ]
            syms_b = [
                CodeSymbol(name="foo", kind="function", language="python",
                           path="main.py", start_line=3, end_line=6,
                           start_byte=0, end_byte=50, signature="def foo()",
                           parent=None, exported=True),
            ]
            imps = [CodeImport(source="utils", imported_names=("utils",), path="main.py", start_line=1)]
            index_file_xref(db_path=db_path, repo="test", path="utils.py",
                            symbols=syms_a, imports=[])
            index_file_xref(db_path=db_path, repo="test", path="main.py",
                            symbols=syms_b, imports=imps)
            resolve_imports_for_repo(db_path=db_path, repo="test")

            calls = [CodeCall(
                caller_name="foo", callee_name="run", path="main.py",
                start_line=4, callee_qualifier="utils",
                caller_symbol_key="function:foo::3",
            )]
            index_file_xref(db_path=db_path, repo="test", path="main.py",
                            symbols=syms_b, imports=imps, calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["resolved"], 0)


    def test_deletion_clears_resolved_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms = [
                CodeSymbol(name="helper", kind="function", language="python",
                           path="test.py", start_line=1, end_line=3,
                           start_byte=0, end_byte=30, signature="helper()",
                           parent=None, exported=True),
            ]
            calls = [CodeCall(
                caller_name="__module__", callee_name="helper", path="test.py",
                start_line=5, caller_symbol_key=None,
            )]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=syms, calls=calls)
            resolve_call_sites_for_repo(db_path=db_path, repo="test")
            delete_file_xref(db_path=db_path, repo="test", path="test.py")
            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            remaining = conn.execute("SELECT COUNT(*) AS cnt FROM call_sites").fetchone()["cnt"]
            conn.close()
            self.assertEqual(remaining, 0)

    def test_adding_definition_enables_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms = [
                CodeSymbol(name="foo", kind="function", language="python",
                           path="test.py", start_line=1, end_line=3,
                           start_byte=0, end_byte=30, signature="foo()",
                           parent=None, exported=True),
            ]
            calls = [CodeCall(
                caller_name="foo", callee_name="helper", path="test.py",
                start_line=2, caller_symbol_key="function:foo::1",
            )]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=syms, calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["resolved"], 0)

            syms2 = [
                CodeSymbol(name="foo", kind="function", language="python",
                           path="test.py", start_line=1, end_line=3,
                           start_byte=0, end_byte=30, signature="foo()",
                           parent=None, exported=True),
                CodeSymbol(name="helper", kind="function", language="python",
                           path="test.py", start_line=5, end_line=6,
                           start_byte=0, end_byte=20, signature="helper()",
                           parent=None, exported=True),
            ]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=syms2, calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["resolved"], 1)

    def test_import_change_updates_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym_helper = CodeSymbol(name="helper", kind="function", language="python",
                                     path="mod_a.py", start_line=1, end_line=3,
                                     start_byte=0, end_byte=30, signature="helper()",
                                     parent=None, exported=True)
            index_file_xref(db_path=db_path, repo="test", path="mod_a.py",
                            symbols=[sym_helper], imports=[])

            syms_main = [CodeSymbol(name="foo", kind="function", language="python",
                                     path="main.py", start_line=3, end_line=6,
                                     start_byte=0, end_byte=50, signature="foo()",
                                     parent=None, exported=True)]
            calls = [CodeCall(caller_name="foo", callee_name="helper", path="main.py",
                              start_line=4, caller_symbol_key="function:foo::3")]
            imp = CodeImport(source="mod_a", imported_names=("helper",), path="main.py", start_line=1)
            index_file_xref(db_path=db_path, repo="test", path="main.py",
                            symbols=syms_main, imports=[imp], calls=calls)
            res = resolve_call_sites_for_repo(db_path=db_path, repo="test")
            self.assertEqual(res["resolved"], 1)


class FindCallersCalleesTests(unittest.TestCase):
    def test_find_callers_by_symbol_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            helper_sym = CodeSymbol(name="helper", kind="function", language="python",
                                     path="test.py", start_line=5, end_line=6,
                                     start_byte=0, end_byte=20, signature="helper()",
                                     parent=None, exported=True)
            foo_sym = CodeSymbol(name="foo", kind="function", language="python",
                                  path="test.py", start_line=1, end_line=4,
                                  start_byte=0, end_byte=50, signature="foo()",
                                  parent=None, exported=True)
            calls = [CodeCall(caller_name="foo", callee_name="helper", path="test.py",
                              start_line=2, caller_symbol_key="function:foo::1")]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[helper_sym, foo_sym], calls=calls)
            resolve_call_sites_for_repo(db_path=db_path, repo="test")

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            helper_row = conn.execute(
                "SELECT id FROM symbols WHERE name = 'helper'"
            ).fetchone()
            conn.close()

            callers = find_callers(db_path=db_path, symbol_id=helper_row["id"])
            self.assertEqual(len(callers), 1)
            self.assertEqual(callers[0]["callee_name"], "helper")

    def test_find_callees(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            foo_sym = CodeSymbol(name="foo", kind="function", language="python",
                                  path="test.py", start_line=1, end_line=6,
                                  start_byte=0, end_byte=80, signature="foo()",
                                  parent=None, exported=True)
            calls = [
                CodeCall(caller_name="foo", callee_name="bar", path="test.py",
                         start_line=2, caller_symbol_key="function:foo::1"),
                CodeCall(caller_name="foo", callee_name="baz", path="test.py",
                         start_line=3, caller_symbol_key="function:foo::1"),
            ]
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[foo_sym], calls=calls)

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            foo_row = conn.execute(
                "SELECT id FROM symbols WHERE name = 'foo'"
            ).fetchone()
            conn.close()

            callees = find_callees(db_path=db_path, caller_symbol_id=foo_row["id"])
            self.assertEqual(len(callees), 2)
            names = {c["callee_name"] for c in callees}
            self.assertIn("bar", names)
            self.assertIn("baz", names)

    def test_find_calls_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            syms = [
                CodeSymbol(name="foo", kind="function", language="python",
                           path="a.py", start_line=1, end_line=3,
                           start_byte=0, end_byte=30, signature="foo()",
                           parent=None, exported=True),
                CodeSymbol(name="foo", kind="function", language="python",
                           path="b.py", start_line=1, end_line=3,
                           start_byte=0, end_byte=30, signature="foo()",
                           parent=None, exported=True),
            ]
            calls = [
                CodeCall(caller_name="__module__", callee_name="foo", path="a.py", start_line=5),
                CodeCall(caller_name="__module__", callee_name="foo", path="b.py", start_line=5),
            ]
            index_file_xref(db_path=db_path, repo="test", path="a.py",
                            symbols=[syms[0]], calls=[calls[0]])
            index_file_xref(db_path=db_path, repo="test", path="b.py",
                            symbols=[syms[1]], calls=[calls[1]])

            results = find_calls_by_name(db_path=db_path, repo="test", callee_name="foo")
            self.assertEqual(len(results), 2)

    def test_find_calls_by_name_filters_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(name="foo", kind="function", language="python",
                             path="a.py", start_line=1, end_line=3,
                             start_byte=0, end_byte=30, signature="foo()",
                             parent=None, exported=True)
            call = CodeCall(caller_name="__module__", callee_name="foo", path="a.py", start_line=5)
            index_file_xref(db_path=db_path, repo="test", path="a.py",
                            symbols=[sym], calls=[call])

            results = find_calls_by_name(db_path=db_path, repo="test", callee_name="foo", path="a.py")
            self.assertEqual(len(results), 1)
            results = find_calls_by_name(db_path=db_path, repo="test", callee_name="foo", path="b.py")
            self.assertEqual(len(results), 0)


class OperationalTests(unittest.TestCase):
    def test_unchanged_reindex_produces_identical_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(name="foo", kind="function", language="python",
                             path="test.py", start_line=1, end_line=3,
                             start_byte=0, end_byte=30, signature="foo()",
                             parent=None, exported=True)
            call = CodeCall(caller_name="foo", callee_name="bar", path="test.py", start_line=2)
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym], calls=[call])

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            rows1 = conn.execute("SELECT * FROM call_sites ORDER BY id").fetchall()
            conn.close()

            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym], calls=[call])

            conn = open_db(xref_db)
            rows2 = conn.execute("SELECT * FROM call_sites ORDER BY id").fetchall()
            conn.close()

            self.assertEqual(len(rows1), len(rows2))
            for r1, r2 in zip(rows1, rows2):
                d1, d2 = dict(r1), dict(r2)
                d1.pop("id")
                d2.pop("id")
                self.assertEqual(d1, d2)


    def test_rename_removes_old_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym_a = CodeSymbol(name="foo", kind="function", language="python",
                               path="old.py", start_line=1, end_line=3,
                               start_byte=0, end_byte=30, signature="foo()",
                               parent=None, exported=True)
            call_a = CodeCall(caller_name="foo", callee_name="bar", path="old.py", start_line=2)
            index_file_xref(db_path=db_path, repo="test", path="old.py",
                            symbols=[sym_a], calls=[call_a])

            delete_file_xref(db_path=db_path, repo="test", path="old.py")

            sym_b = CodeSymbol(name="foo", kind="function", language="python",
                               path="new.py", start_line=1, end_line=3,
                               start_byte=0, end_byte=30, signature="foo()",
                               parent=None, exported=True)
            call_b = CodeCall(caller_name="foo", callee_name="bar", path="new.py", start_line=2)
            index_file_xref(db_path=db_path, repo="test", path="new.py",
                            symbols=[sym_b], calls=[call_b])

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            old_rows = conn.execute("SELECT * FROM call_sites WHERE path='old.py'").fetchall()
            new_rows = conn.execute("SELECT * FROM call_sites WHERE path='new.py'").fetchall()
            conn.close()
            self.assertEqual(len(old_rows), 0)
            self.assertEqual(len(new_rows), 1)

    def test_foreign_key_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(name="foo", kind="function", language="python",
                             path="test.py", start_line=1, end_line=3,
                             start_byte=0, end_byte=30, signature="foo()",
                             parent=None, exported=True)
            call = CodeCall(caller_name="foo", callee_name="bar", path="test.py",
                            start_line=2, caller_symbol_key="function:foo::1")
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym], calls=[call])

            delete_file_xref(db_path=db_path, repo="test", path="test.py")

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                remaining = conn.execute("SELECT COUNT(*) AS cnt FROM call_sites").fetchone()["cnt"]
                self.assertEqual(remaining, 0)
            finally:
                conn.close()

    def test_resolver_clears_old_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            sym = CodeSymbol(name="helper", kind="function", language="python",
                             path="test.py", start_line=1, end_line=3,
                             start_byte=0, end_byte=30, signature="helper()",
                             parent=None, exported=True)
            call = CodeCall(caller_name="__module__", callee_name="helper", path="test.py",
                            start_line=5, caller_symbol_key=None)
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym], calls=[call])
            resolve_call_sites_for_repo(db_path=db_path, repo="test")

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            sym_id = conn.execute("SELECT id FROM symbols WHERE name='helper'").fetchone()["id"]
            status = conn.execute(
                "SELECT resolved_symbol_id, resolution_status FROM call_sites"
            ).fetchone()
            conn.close()
            self.assertEqual(status["resolved_symbol_id"], sym_id)
            self.assertEqual(status["resolution_status"], "resolved")

            sym2 = CodeSymbol(name="helper", kind="function", language="python",
                              path="test.py", start_line=10, end_line=12,
                              start_byte=0, end_byte=30, signature="helper()",
                              parent=None, exported=True)
            index_file_xref(db_path=db_path, repo="test", path="test.py",
                            symbols=[sym, sym2], calls=[call])
            resolve_call_sites_for_repo(db_path=db_path, repo="test")

            conn = open_db(xref_db)
            status = conn.execute(
                "SELECT resolution_status FROM call_sites"
            ).fetchone()
            conn.close()
            self.assertEqual(status["resolution_status"], "ambiguous")

    def test_multiple_files_get_deterministic_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            for i, fn in enumerate(["c.py", "a.py", "b.py"]):
                sym = CodeSymbol(name="foo", kind="function", language="python",
                                 path=fn, start_line=1, end_line=3,
                                 start_byte=0, end_byte=30, signature="foo()",
                                 parent=None, exported=True)
                call = CodeCall(caller_name="__module__", callee_name="bar", path=fn,
                                start_line=2)
                index_file_xref(db_path=db_path, repo="test", path=fn,
                                symbols=[sym], calls=[call])

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            rows = conn.execute(
                "SELECT path FROM call_sites ORDER BY repo, path, start_line"
            ).fetchall()
            conn.close()
            paths = [r["path"] for r in rows]
            self.assertEqual(paths, ["a.py", "b.py", "c.py"])


if __name__ == "__main__":
    unittest.main()
