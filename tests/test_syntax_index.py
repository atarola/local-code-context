from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_rag.languages import detect_language  # noqa: E402
from local_code_rag.syntax_index import (  # noqa: E402
    MAX_SIGNATURE_CHARS,
    BuildResult,
    ParseQuality,
    build_index_records,
    compare_python_extractions,
    evaluate_parse_quality,
    extract_python_imports,
    extract_python_symbols,
    make_chunk_id,
)
from local_code_rag.syntax_query import (  # noqa: E402
    PythonTagQueryExtractor,
    load_python_tags_query,
)
from local_code_rag.tree_sitter_support import ParserRegistry  # noqa: E402


@dataclass
class FakePoint:
    row: int
    column: int = 0


class FakeNode:
    def __init__(
        self,
        type_name: str,
        start_byte: int,
        end_byte: int,
        start_row: int,
        end_row: int,
        *,
        children: list["FakeNode"] | None = None,
        field_map: dict[str, "FakeNode"] | None = None,
        is_missing: bool = False,
    ) -> None:
        self.type = type_name
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.start_point = FakePoint(start_row)
        self.end_point = FakePoint(end_row)
        self.children = children or []
        self.named_children = list(self.children)
        self._field_map = field_map or {}
        self.is_missing = is_missing
        self.parent: FakeNode | None = None
        for child in self.children:
            child.parent = self

    def child_by_field_name(self, name: str) -> "FakeNode" | None:
        return self._field_map.get(name)


class FakeTree:
    def __init__(self, root_node: FakeNode) -> None:
        self.root_node = root_node


class FakeParser:
    def __init__(
        self, tree: FakeTree | None = None, exc: Exception | None = None
    ) -> None:
        self.tree = tree
        self.exc = exc
        self.calls = 0

    def parse(self, source: bytes) -> FakeTree:  # noqa: ARG002
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        if self.tree is None:
            raise RuntimeError("missing fake tree")
        return self.tree


class FakeRegistry:
    def __init__(self, parser: FakeParser | None) -> None:
        self.parser = parser
        self.calls: list[str] = []

    def get(self, language: str):
        self.calls.append(language)
        if language == "python":
            return self.parser
        return None


class FakeCaptureSource:
    def __init__(self, captures: list[tuple[str, FakeNode]]) -> None:
        self._captures = captures

    def captures(self, tree: object):  # noqa: ARG002
        return list(self._captures)


def _line_offsets(text: str) -> list[tuple[int, int]]:
    offsets: list[tuple[int, int]] = []
    position = 0
    for line in text.splitlines(keepends=True):
        offsets.append((position, position + len(line)))
        position += len(line)
    if text and not text.endswith("\n"):
        offsets.append((position, position))
    return offsets


def _line_span(
    offsets: list[tuple[int, int]], start_line: int, end_line: int
) -> tuple[int, int]:
    return offsets[start_line - 1][0], offsets[end_line - 1][1]


def _sample_source() -> str:
    return (
        "import os\n"
        "from pkg.mod import thing, other as alias\n"
        "\n"
        "CONST = 1\n"
        "\n"
        "@decorator\n"
        "class RepositoryIndex(Base):\n"
        "    @cache\n"
        "    def replace_path(self, path):\n"
        "        return path\n"
        "\n"
        "@trace\n"
        "async def run_server(host, port):\n"
        "    return host, port\n"
        "\n"
        "def helper():\n"
        "    return 2\n"
    )


def _sample_tree() -> FakeTree:
    source = _sample_source()
    offsets = _line_offsets(source)
    line1 = _line_span(offsets, 1, 1)
    line2 = _line_span(offsets, 2, 2)
    line4 = _line_span(offsets, 4, 4)
    line6_10 = _line_span(offsets, 6, 10)
    line7_10 = _line_span(offsets, 7, 10)
    line8_10 = _line_span(offsets, 8, 10)
    line9_10 = _line_span(offsets, 9, 10)
    line12_14 = _line_span(offsets, 12, 14)
    line13_14 = _line_span(offsets, 13, 14)
    line16_17 = _line_span(offsets, 16, 17)

    method_fn = FakeNode("function_definition", *line9_10, 9, 10)
    method = FakeNode("decorated_definition", *line8_10, 8, 10, children=[method_fn])
    class_block = FakeNode("block", *line7_10, 7, 10, children=[method])
    class_node = FakeNode(
        "class_definition",
        *line7_10,
        7,
        10,
        children=[class_block],
        field_map={"body": class_block},
    )
    decorated_class = FakeNode(
        "decorated_definition",
        *line6_10,
        6,
        10,
        children=[class_node],
    )
    async_fn = FakeNode("function_definition", *line13_14, 13, 14)
    decorated_async = FakeNode(
        "decorated_definition",
        *line12_14,
        12,
        14,
        children=[async_fn],
    )
    helper = FakeNode("function_definition", *line16_17, 16, 17)
    imp1 = FakeNode("import_statement", *line1, 1, 1)
    imp2 = FakeNode("import_from_statement", *line2, 2, 2)
    const = FakeNode("assignment", *line4, 4, 4)
    root = FakeNode(
        "module",
        0,
        len(source.encode("utf-8")),
        1,
        17,
        children=[imp1, imp2, const, decorated_class, decorated_async, helper],
    )
    return FakeTree(root)


def _malformed_tree() -> FakeTree:
    source = b"def broken(:\n    pass\n"
    error = FakeNode("ERROR", 0, len(source), 1, 2)
    root = FakeNode("module", 0, len(source), 1, 2, children=[error])
    return FakeTree(root)


class SyntaxIndexTests(unittest.TestCase):
    def test_detect_language_py_suffix(self) -> None:
        self.assertEqual(detect_language(Path("demo.py"), b""), "python")

    def test_detect_language_pyi_suffix(self) -> None:
        self.assertEqual(detect_language(Path("demo.pyi"), b""), "python")

    def test_detect_language_python_shebang(self) -> None:
        self.assertEqual(
            detect_language(Path("script"), b"#!/usr/bin/env python3\nprint(1)\n"),
            "python",
        )

    def test_detect_language_unknown(self) -> None:
        self.assertIsNone(detect_language(Path("notes.txt"), b"hello\n"))

    def test_lazy_parser_loading(self) -> None:
        fake_parser = FakeParser(_sample_tree())
        registry = ParserRegistry()
        with patch(
            "local_code_rag.tree_sitter_support._build_python_parser",
            return_value=fake_parser,
        ) as build:
            first = registry.get("python")
            second = registry.get("python")

        self.assertIs(first, fake_parser)
        self.assertIs(second, fake_parser)
        self.assertEqual(build.call_count, 1)

    def test_unavailable_parser_falls_back_to_text(self) -> None:
        source = _sample_source().encode("utf-8")
        registry = FakeRegistry(None)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_bytes(source)
            result = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=_sample_source(),
                parser_registry=registry,
            )

        self.assertIsInstance(result, BuildResult)
        self.assertTrue(result.records)
        self.assertTrue(
            all(record.metadata["chunk_type"] == "text" for record in result.records)
        )
        self.assertEqual(result.language, "python")

    def test_clean_python_parse_quality(self) -> None:
        tree = _sample_tree()
        source = _sample_source().encode("utf-8")
        quality = evaluate_parse_quality(tree, source)
        self.assertIsInstance(quality, ParseQuality)
        self.assertTrue(quality.usable)
        self.assertEqual(quality.error_nodes, 0)

    def test_malformed_python_parse_quality(self) -> None:
        tree = _malformed_tree()
        source = b"def broken(:\n    pass\n"
        quality = evaluate_parse_quality(tree, source)
        self.assertFalse(quality.usable)
        self.assertGreaterEqual(quality.error_nodes, 1)

    def test_function_class_method_and_decorated_extraction(self) -> None:
        source = _sample_source().encode("utf-8")
        tree = _sample_tree()
        symbols = extract_python_symbols(source, tree, "demo.py")
        kinds = [(item.kind, item.name, item.parent) for item in symbols]
        self.assertIn(("class", "RepositoryIndex", None), kinds)
        self.assertIn(("method", "replace_path", "RepositoryIndex"), kinds)
        self.assertIn(("function", "run_server", None), kinds)
        self.assertIn(("function", "helper", None), kinds)

    def test_async_function_extraction(self) -> None:
        source = _sample_source().encode("utf-8")
        tree = _sample_tree()
        symbols = extract_python_symbols(source, tree, "demo.py")
        async_symbol = next(item for item in symbols if item.name == "run_server")
        self.assertTrue(async_symbol.signature.startswith("async def run_server"))

    def test_import_extraction(self) -> None:
        source = _sample_source().encode("utf-8")
        tree = _sample_tree()
        imports = extract_python_imports(source, tree, "demo.py")
        self.assertEqual(len(imports), 2)
        self.assertEqual(imports[0].source, "os")
        self.assertEqual(imports[1].source, "pkg.mod")
        self.assertEqual(imports[1].imported_names, ("thing", "other"))

    def test_python_tags_query_loader(self) -> None:
        query = load_python_tags_query()
        self.assertIsInstance(query, str)
        self.assertIn("definition.class", query)
        self.assertIn("definition.function", query)
        self.assertIn("reference.call", query)

    def test_generic_query_extractor_matches_legacy_fixture(self) -> None:
        source = _sample_source().encode("utf-8")
        tree = _sample_tree()
        root = tree.root_node
        captures = [
            ("definition.module", root),
            ("reference.import", root.children[0]),
            ("reference.import", root.children[1]),
            ("definition.class", root.children[3]),
            ("definition.method", root.children[3].children[0].children[0].children[0]),
            ("definition.function", root.children[4]),
            ("definition.function", root.children[5]),
        ]
        extractor = PythonTagQueryExtractor(capture_source=FakeCaptureSource(captures))
        legacy_symbols = extract_python_symbols(source, tree, "demo.py")
        legacy_imports = extract_python_imports(source, tree, "demo.py")
        query = extractor.extract(source, tree, "demo.py")

        self.assertEqual(
            [(item.kind, item.name, item.parent) for item in query.symbols],
            [(item.kind, item.name, item.parent) for item in legacy_symbols],
        )
        self.assertEqual(
            [
                (item.source, item.imported_names, item.start_line)
                for item in query.imports
            ],
            [
                (item.source, item.imported_names, item.start_line)
                for item in legacy_imports
            ],
        )

    def test_query_comparison_reports_parity_gaps(self) -> None:
        source = _sample_source().encode("utf-8")
        tree = _sample_tree()
        root = tree.root_node
        captures = [
            ("definition.class", root.children[3]),
            ("definition.function", root.children[4]),
        ]
        comparison = compare_python_extractions(
            source,
            tree,
            "demo.py",
            capture_source=FakeCaptureSource(captures),
        )

        self.assertTrue(comparison.gaps)
        self.assertTrue(any(gap.field == "symbol names" for gap in comparison.gaps))

    def test_query_mode_matches_legacy_records_when_captures_match(self) -> None:
        source = _sample_source().encode("utf-8")
        tree = _sample_tree()
        root = tree.root_node
        captures = [
            ("definition.module", root),
            ("reference.import", root.children[0]),
            ("reference.import", root.children[1]),
            ("definition.class", root.children[3]),
            ("definition.method", root.children[3].children[0].children[0].children[0]),
            ("definition.function", root.children[4]),
            ("definition.function", root.children[5]),
        ]
        registry = FakeRegistry(FakeParser(tree))
        query_extractor = PythonTagQueryExtractor(
            capture_source=FakeCaptureSource(captures)
        )
        original = build_index_records(
            repo="demo",
            repo_root=Path("/tmp/demo"),
            path=Path("/tmp/demo/demo.py"),
            source=source,
            text=_sample_source(),
            parser_registry=registry,
        )
        with patch.dict(
            "local_code_rag.syntax_index.QUERY_EXTRACTORS",
            {"python": query_extractor},
        ):
            queried = build_index_records(
                repo="demo",
                repo_root=Path("/tmp/demo"),
                path=Path("/tmp/demo/demo.py"),
                source=source,
                text=_sample_source(),
                parser_registry=registry,
                python_extractor_mode="query",
            )

        self.assertEqual(
            [record.id for record in original.records],
            [record.id for record in queried.records],
        )
        self.assertEqual(
            [record.document for record in original.records],
            [record.document for record in queried.records],
        )

    def test_query_mode_falls_back_to_legacy_on_query_failure(self) -> None:
        source = _sample_source().encode("utf-8")
        tree = _sample_tree()
        registry = FakeRegistry(FakeParser(tree))
        query_extractor = PythonTagQueryExtractor(capture_source=FakeCaptureSource([]))
        with patch.dict(
            "local_code_rag.syntax_index.QUERY_EXTRACTORS",
            {"python": query_extractor},
        ):
            result = build_index_records(
                repo="demo",
                repo_root=Path("/tmp/demo"),
                path=Path("/tmp/demo/demo.py"),
                source=source,
                text=_sample_source(),
                parser_registry=registry,
                python_extractor_mode="query",
            )

        self.assertTrue(result.records)
        self.assertTrue(
            any(
                record.metadata["chunk_type"] == "file_map" for record in result.records
            )
        )

    def test_compact_signatures(self) -> None:
        long_args = ", ".join(f"arg{i}" for i in range(50))
        source = f"def very_long({long_args}):\n    return 1\n".encode("utf-8")
        function = FakeNode(
            "function_definition",
            0,
            len(source),
            1,
            2,
        )
        tree = FakeTree(FakeNode("module", 0, len(source), 1, 2, children=[function]))
        symbols = extract_python_symbols(source, tree, "demo.py")
        self.assertLessEqual(len(symbols[0].signature or ""), MAX_SIGNATURE_CHARS)

    def test_python_symbol_records(self) -> None:
        source = _sample_source().encode("utf-8")
        registry = FakeRegistry(FakeParser(_sample_tree()))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_bytes(source)
            result = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=_sample_source(),
                parser_registry=registry,
            )

        kinds = [record.metadata["chunk_type"] for record in result.records]
        self.assertIn("file_map", kinds)
        self.assertIn("symbol", kinds)
        self.assertTrue(
            any("RepositoryIndex" in record.document for record in result.records)
        )

    def test_python_file_map_record(self) -> None:
        source = _sample_source().encode("utf-8")
        registry = FakeRegistry(FakeParser(_sample_tree()))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_bytes(source)
            result = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=_sample_source(),
                parser_registry=registry,
            )

        file_map = next(
            record
            for record in result.records
            if record.metadata["chunk_type"] == "file_map"
        )
        self.assertIn("File: demo.py", file_map.document)
        self.assertIn("Imports:", file_map.document)
        self.assertIn("Symbols:", file_map.document)

    def test_oversized_symbol_splitting(self) -> None:
        body_lines = "\n".join(f"    value_{i} = {i}" for i in range(90))
        source = f"def huge():\n{body_lines}\n".encode("utf-8")
        function = FakeNode("function_definition", 0, len(source), 1, 91)
        tree = FakeTree(FakeNode("module", 0, len(source), 1, 91, children=[function]))
        registry = FakeRegistry(FakeParser(tree))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_bytes(source)
            result = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=source.decode("utf-8"),
                parser_registry=registry,
            )

        self.assertTrue(
            any(
                record.metadata["chunk_type"] == "symbol_part"
                for record in result.records
            )
        )

    def test_stable_structural_record_ids(self) -> None:
        source = _sample_source().encode("utf-8")
        registry = FakeRegistry(FakeParser(_sample_tree()))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_bytes(source)
            first = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=_sample_source(),
                parser_registry=registry,
            )
            second = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=_sample_source(),
                parser_registry=registry,
            )

        self.assertEqual(
            [record.id for record in first.records],
            [record.id for record in second.records],
        )

    def test_malformed_python_text_fallback(self) -> None:
        source = b"def broken(:\n    pass\n"
        registry = FakeRegistry(FakeParser(_malformed_tree()))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_bytes(source)
            result = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=source.decode("utf-8", errors="replace"),
                parser_registry=registry,
            )

        self.assertTrue(result.records)
        self.assertTrue(
            all(record.metadata["chunk_type"] == "text" for record in result.records)
        )
        self.assertEqual(result.records[0].metadata["language"], "python")

    def test_unknown_file_text_fallback(self) -> None:
        source = b"hello world\nthis is a note\n"
        registry = FakeRegistry(None)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "notes.txt"
            path.write_bytes(source)
            result = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=source.decode("utf-8"),
                parser_registry=registry,
            )

        self.assertTrue(result.records)
        self.assertTrue(
            all(record.metadata["chunk_type"] == "text" for record in result.records)
        )
        self.assertEqual(result.records[0].metadata["language"], "")

    def test_text_chunks_get_unique_ids(self) -> None:
        source = "\n".join(f"line {index}" for index in range(200)).encode("utf-8")
        registry = FakeRegistry(None)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "notes.txt"
            path.write_bytes(source)
            result = build_index_records(
                repo="demo",
                repo_root=root,
                path=path,
                source=source,
                text=source.decode("utf-8"),
                parser_registry=registry,
            )

        ids = [record.id for record in result.records]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreater(len(ids), 1)

    def test_stable_id_helper_includes_symbol_metadata(self) -> None:
        self.assertEqual(
            make_chunk_id("demo", "demo.py", "symbol", "run", "Class", 0),
            make_chunk_id("demo", "demo.py", "symbol", "run", "Class", 0),
        )
        self.assertNotEqual(
            make_chunk_id("demo", "demo.py", "symbol", "run", "Class", 0),
            make_chunk_id("demo", "demo.py", "symbol", "run", "Class", 1),
        )


if __name__ == "__main__":
    unittest.main()
