from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_context.mcp.symbols import get_symbol  # noqa: E402


class FakeSymbolCollection:
    def __init__(
        self, records: list[tuple[str, dict[str, Any]]]
    ):
        self._records = records

    def get(self, where=None, limit=None, include=None, offset=None):  # noqa: ANN001
        filtered = self._records
        if where and "$and" in where:
            for cond in where["$and"]:
                for field, op_val in cond.items():
                    if isinstance(op_val, dict) and "$eq" in op_val:
                        filtered = [
                            r for r in filtered if r[1].get(field) == op_val["$eq"]
                        ]
                    elif isinstance(op_val, dict) and "$in" in op_val:
                        filtered = [
                            r for r in filtered if r[1].get(field) in op_val["$in"]
                        ]
        if limit is not None:
            filtered = filtered[:limit]

        result: dict[str, list[Any]] = {}
        if not include or "metadatas" in include:
            result["metadatas"] = [r[1] for r in filtered]
        if not include or "documents" in include:
            result["documents"] = [r[0] for r in filtered]
        return result


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        db=Path("/tmp/test_db"),
        collection="test_chunks",
        top_k=5,
        embed_model="nomic-embed-text",
        model="qwen2.5-coder:14b",
        ollama_url="http://localhost:11434",
        repo=None,
    )


class GetSymbolTests(unittest.TestCase):
    def test_not_found_returns_explicit_message(self) -> None:
        fake = FakeSymbolCollection([])
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="non_existent")
        self.assertIn("not found", result)
        self.assertIn("non_existent", result)

    def test_duplicate_symbol_across_repositories(self) -> None:
        fake = FakeSymbolCollection([
            (
                "def init():\n    pass\n",
                {
                    "repo": "project_a",
                    "path": "src/core.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "init",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 1,
                    "end_line": 2,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
            (
                "def init():\n    return 0\n",
                {
                    "repo": "project_b",
                    "path": "src/setup.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "init",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 5,
                    "end_line": 6,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
        ])
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="init")
        self.assertIn("project_a", result)
        self.assertIn("project_b", result)
        self.assertIn("src/core.py", result)
        self.assertIn("src/setup.py", result)

    def test_duplicate_symbol_within_one_file(self) -> None:
        fake = FakeSymbolCollection([
            (
                "def reset():\n    return 1\n",
                {
                    "repo": "project_a",
                    "path": "src/device.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "reset",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 10,
                    "end_line": 11,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
            (
                "def reset():\n    return 2\n",
                {
                    "repo": "project_a",
                    "path": "src/device.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "reset",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 20,
                    "end_line": 21,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
        ])
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="reset")
        self.assertIn("10-11", result)
        self.assertIn("20-21", result)
        self.assertEqual(result.count("Repository:"), 2)

    def test_methods_distinguished_by_parent_symbol(self) -> None:
        fake = FakeSymbolCollection([
            (
                "def handle(self):\n    pass\n",
                {
                    "repo": "project_a",
                    "path": "src/handler.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "handle",
                    "symbol_kind": "method",
                    "parent_symbol": "InputHandler",
                    "start_line": 15,
                    "end_line": 16,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
            (
                "def handle(self, event):\n    pass\n",
                {
                    "repo": "project_a",
                    "path": "src/handler.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "handle",
                    "symbol_kind": "method",
                    "parent_symbol": "OutputHandler",
                    "start_line": 30,
                    "end_line": 31,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
        ])
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="handle")
        self.assertIn("InputHandler", result)
        self.assertIn("OutputHandler", result)
        self.assertEqual(result.count("Repository:"), 2)

    def test_complete_multipart_reconstruction(self) -> None:
        fake = FakeSymbolCollection([
            (
                "line 1\nline 2\nline 3\nline 4\nline 5",
                {
                    "repo": "project_a",
                    "path": "src/large.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big_func",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 10,
                    "end_line": 14,
                    "part_index": 1,
                    "part_count": 3,
                },
            ),
            (
                "line 6\nline 7\nline 8\nline 9\nline 10",
                {
                    "repo": "project_a",
                    "path": "src/large.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big_func",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 15,
                    "end_line": 19,
                    "part_index": 2,
                    "part_count": 3,
                },
            ),
            (
                "line 11\nline 12\nline 13\nline 14\nline 15",
                {
                    "repo": "project_a",
                    "path": "src/large.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big_func",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 20,
                    "end_line": 24,
                    "part_index": 3,
                    "part_count": 3,
                },
            ),
        ])
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="big_func")
        self.assertIn("Part 1/3", result)
        self.assertIn("Part 2/3", result)
        self.assertIn("Part 3/3", result)
        self.assertIn("10-24", result)
        self.assertNotIn("Warning", result)

    def test_incomplete_multipart_reports_warning(self) -> None:
        fake = FakeSymbolCollection([
            (
                "part one content",
                {
                    "repo": "project_a",
                    "path": "src/large.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big_func",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 10,
                    "end_line": 12,
                    "part_index": 1,
                    "part_count": 4,
                },
            ),
            (
                "part two content",
                {
                    "repo": "project_a",
                    "path": "src/large.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big_func",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 13,
                    "end_line": 15,
                    "part_index": 2,
                    "part_count": 4,
                },
            ),
        ])
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="big_func")
        self.assertIn("Warning", result)
        self.assertIn("missing", result)

    def test_deterministic_ordering_by_repo_then_path(self) -> None:
        records = [
            (
                "def c():\n    pass\n",
                {
                    "repo": "repo_b",
                    "path": "a.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "my_sym",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 30,
                    "end_line": 31,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
            (
                "def b():\n    pass\n",
                {
                    "repo": "repo_a",
                    "path": "z.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "my_sym",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 20,
                    "end_line": 21,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
            (
                "def a():\n    pass\n",
                {
                    "repo": "repo_a",
                    "path": "a.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "my_sym",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 10,
                    "end_line": 11,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
        ]
        import random
        for _ in range(5):
            random.shuffle(records)
            fake = FakeSymbolCollection(list(records))
            with patch(
                "local_code_context.mcp.symbols.get_collection", return_value=fake
            ):
                result = get_symbol(_config(), symbol="my_sym")
            a_pos = result.index("repo_a")
            b_pos = result.index("repo_b")
            self.assertLess(a_pos, b_pos)
            # Within repo_a, a.py (line 10) comes before z.py (line 20)
            a_py_pos = result.index("a.py")
            z_py_pos = result.index("z.py")
            self.assertLess(a_py_pos, z_py_pos)

    def test_colliding_multipart_symbols_detected_separately(self) -> None:
        records_a = [
            (
                "def big():\n    x = 1\n",
                {
                    "repo": "project_a",
                    "path": "src/cond.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 10,
                    "end_line": 11,
                    "part_index": 1,
                    "part_count": 2,
                },
            ),
            (
                "def big():\n    y = 2\n",
                {
                    "repo": "project_a",
                    "path": "src/cond.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 12,
                    "end_line": 13,
                    "part_index": 2,
                    "part_count": 2,
                },
            ),
        ]
        records_b = [
            (
                "def big():\n    a = 1\n",
                {
                    "repo": "project_a",
                    "path": "src/cond.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 50,
                    "end_line": 51,
                    "part_index": 1,
                    "part_count": 2,
                },
            ),
            (
                "def big():\n    b = 2\n",
                {
                    "repo": "project_a",
                    "path": "src/cond.py",
                    "language": "python",
                    "chunk_type": "symbol_part",
                    "symbol": "big",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 52,
                    "end_line": 53,
                    "part_index": 2,
                    "part_count": 2,
                },
            ),
        ]
        fake = FakeSymbolCollection(records_a + records_b)
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="big")

        self.assertEqual(result.count("Part 1/2"), 2)
        self.assertEqual(result.count("Part 2/2"), 2)
        self.assertIn("ambiguous symbol key", result)
        # Both definitions appear as separate entries
        self.assertIn("10-13", result)
        self.assertIn("50-53", result)

    def test_limit_applies_after_reconstruction(self) -> None:
        records = [
            (
                f"def sym{i}():\n    pass\n",
                {
                    "repo": "project_a",
                    "path": "src/mod.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "sym",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": i * 10 + 1,
                    "end_line": i * 10 + 2,
                    "part_index": 0,
                    "part_count": 1,
                },
            )
            for i in range(10)
        ]
        fake = FakeSymbolCollection(records)
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="sym", limit=3)
        self.assertIn("=== Symbol: sym ===", result)
        self.assertEqual(result.count("Repository:"), 3)

    def test_repo_filter_narrows_results(self) -> None:
        fake = FakeSymbolCollection([
            (
                "def util():\n    pass\n",
                {
                    "repo": "repo_a",
                    "path": "src/util.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "util",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 1,
                    "end_line": 2,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
            (
                "def util():\n    return 1\n",
                {
                    "repo": "repo_b",
                    "path": "src/util.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "util",
                    "symbol_kind": "function",
                    "parent_symbol": "",
                    "start_line": 5,
                    "end_line": 6,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
        ])
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="util", repo="repo_a")
        self.assertIn("repo_a", result)
        self.assertNotIn("repo_b", result)

    def test_kind_filter_narrows_results(self) -> None:
        fake = FakeSymbolCollection([
            (
                "class Config:\n    pass\n",
                {
                    "repo": "repo_a",
                    "path": "src/config.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "Config",
                    "symbol_kind": "class",
                    "parent_symbol": "",
                    "start_line": 1,
                    "end_line": 2,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
            (
                "Config = {}",
                {
                    "repo": "repo_a",
                    "path": "src/config.py",
                    "language": "python",
                    "chunk_type": "symbol",
                    "symbol": "Config",
                    "symbol_kind": "constant",
                    "parent_symbol": "",
                    "start_line": 5,
                    "end_line": 5,
                    "part_index": 0,
                    "part_count": 1,
                },
            ),
        ])
        with patch(
            "local_code_context.mcp.symbols.get_collection", return_value=fake
        ):
            result = get_symbol(_config(), symbol="Config", kind="class")
        self.assertIn("class", result)
        self.assertNotIn("constant", result)


if __name__ == "__main__":
    unittest.main()
