from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_context.mcp import context as mcp_context  # noqa: E402
from local_code_context.mcp import server as mcp_server  # noqa: E402


class FakeCollection:
    def __init__(self, metadatas: list[dict[str, object]]):
        self._metadatas = metadatas

    def get(self, include=None):  # noqa: ANN001
        return {"metadatas": self._metadatas}


def _config(tmpdir: str) -> SimpleNamespace:
    return SimpleNamespace(
        db=Path(tmpdir),
        collection="code_chunks",
        top_k=5,
        embed_model="nomic-embed-text",
        model="qwen2.5-coder:14b",
        ollama_url="http://localhost:11434",
        repo=None,
    )


class MCPContextTests(unittest.TestCase):
    def test_repository_metadata_discovery(self) -> None:
        fake = FakeCollection(
            [
                {"repo": "alpha", "repo_root": "/tmp/alpha", "path": "a.py"},
                {"repo": "alpha", "repo_root": "/tmp/alpha", "path": "b.py"},
                {"repo": "beta", "repo_root": "/tmp/beta", "path": "c.py"},
            ]
        )
        config = _config("/tmp/db")
        with patch("local_code_context.mcp.context._open_collection", return_value=fake):
            self.assertEqual(
                mcp_context.list_indexed_repositories(config), ["alpha", "beta"]
            )

    def test_repository_root_validation(self) -> None:
        fake = FakeCollection([{"repo": "alpha", "path": "a.py"}])
        config = _config("/tmp/db")
        with patch("local_code_context.mcp.context._open_collection", return_value=fake):
            with self.assertRaisesRegex(ValueError, "repository root is missing"):
                mcp_context.get_repository_context(config, "alpha")

    def test_missing_repository_handling(self) -> None:
        fake = FakeCollection(
            [{"repo": "alpha", "repo_root": "/tmp/alpha", "path": "a.py"}]
        )
        config = _config("/tmp/db")
        with patch("local_code_context.mcp.context._open_collection", return_value=fake):
            with self.assertRaisesRegex(ValueError, "is not indexed"):
                mcp_context.get_repository_context(config, "beta")

    def test_old_index_records_without_repo_root(self) -> None:
        fake = FakeCollection([{"repo": "alpha", "path": "a.py"}])
        config = _config("/tmp/db")
        with patch("local_code_context.mcp.context._open_collection", return_value=fake):
            self.assertEqual(mcp_context.list_indexed_repositories(config), ["alpha"])
            with self.assertRaisesRegex(ValueError, "repository root is missing"):
                mcp_context.get_repository_context(config, "alpha")

    def test_path_traversal_rejection(self) -> None:
        root = Path("/tmp/example")
        with self.assertRaises(ValueError):
            mcp_context._resolve_repo_path(root, "../secret")  # noqa: SLF001

    def test_file_tree_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("readme", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                "[project]\nname='demo'\n", encoding="utf-8"
            )
            (root / "src").mkdir()
            (root / "src" / "pkg").mkdir()
            (root / "src" / "pkg" / "module.py").write_text(
                "print('x')\n", encoding="utf-8"
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_module.py").write_text(
                "assert True\n", encoding="utf-8"
            )

            files = list(root.rglob("*"))
            tree_paths = mcp_context._priority_tree_paths(files, root, 20)  # noqa: SLF001
            tree = mcp_context._ascii_tree(tree_paths, root)  # noqa: SLF001

            self.assertIn("README.md", tree)
            self.assertIn("pyproject.toml", tree)
            self.assertIn("src/", tree)
            self.assertIn("tests/", tree)

    def test_important_file_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("readme", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                "[project]\nname='demo'\n", encoding="utf-8"
            )
            (root / "notes.txt").write_text("ignore", encoding="utf-8")

            files = list(root.rglob("*"))
            manifests = mcp_context._manifest_files(root, files)  # noqa: SLF001
            names = {path.name for path in manifests}
            self.assertIn("README.md", names)
            self.assertIn("pyproject.toml", names)

    def test_output_truncation(self) -> None:
        text = mcp_context._render_sections(
            [("One", "x" * 2_000), ("Two", "y" * 2_000)], 120
        )  # noqa: SLF001
        self.assertLessEqual(len(text), 120)
        self.assertIn("[Context truncated]", text)

    def test_get_repository_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("# Demo\n\nSummary.", encoding="utf-8")
            (root / "pyproject.toml").write_text(
                "[project]\nname='demo'\n[project.scripts]\ndemo='demo:main'\n",
                encoding="utf-8",
            )
            (root / "src").mkdir()
            (root / "src" / "demo.py").write_text(
                "def main():\n    return 1\n", encoding="utf-8"
            )
            (root / "tests").mkdir()
            (root / "tests" / "test_demo.py").write_text(
                "assert True\n", encoding="utf-8"
            )

            fake = FakeCollection(
                [
                    {"repo": "demo", "repo_root": str(root), "path": "README.md"},
                    {"repo": "demo", "repo_root": str(root), "path": "pyproject.toml"},
                ]
            )
            config = _config("/tmp/db")
            with patch(
                "local_code_context.mcp.context._open_collection", return_value=fake
            ):
                text = mcp_context.get_repository_context(
                    config, "demo", max_chars=10_000
                )
            self.assertIn("=== Repository ===", text)
            self.assertIn("=== File tree ===", text)
            self.assertIn("=== README ===", text)
            self.assertIn("=== Entry points ===", text)
            self.assertIn("demo:main", text)

    def test_get_workspace_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            repo_a = base / "alpha"
            repo_b = base / "beta"
            repo_a.mkdir()
            repo_b.mkdir()
            (repo_a / "README.md").write_text("alpha", encoding="utf-8")
            (repo_b / "README.md").write_text("beta", encoding="utf-8")
            fake = FakeCollection(
                [
                    {"repo": "alpha", "repo_root": str(repo_a), "path": "README.md"},
                    {"repo": "beta", "repo_root": str(repo_b), "path": "README.md"},
                ]
            )
            config = _config("/tmp/db")
            with patch(
                "local_code_context.mcp.context._open_collection", return_value=fake
            ):
                text = mcp_context.get_workspace_context(
                    config, max_chars_per_repo=2_000
                )
            self.assertIn("=== Repository ===", text)
            self.assertIn("alpha", text)
            self.assertIn("beta", text)

    def test_search_code_with_and_without_repo(self) -> None:
        config = _config("/tmp/db")
        calls: list[tuple[str | None, str]] = []

        def fake_search_chunks(**kwargs):  # noqa: ANN001
            calls.append((kwargs.get("repo"), kwargs["query"]))
            return [
                {
                    "document": "def sample():\n    return 1",
                    "metadata": {
                        "repo": kwargs.get("repo") or "alpha",
                        "path": "src/demo.py",
                        "start_line": 1,
                        "end_line": 2,
                    },
                    "distance": 0.1234,
                }
            ]

        with patch(
            "local_code_context.mcp.context.search_chunks", side_effect=fake_search_chunks
        ):
            with patch(
                "local_code_context.retrieval.query.ollama_chat",
                side_effect=AssertionError("should not be called"),
            ):
                text_all = mcp_context.search_code(config, q="sample query")
                text_repo = mcp_context.search_code(
                    config, q="sample query", repo="alpha"
                )

        self.assertIn("=== Search query ===", text_all)
        self.assertIn("=== Retrieved sources ===", text_all)
        self.assertIn("alpha:src/demo.py:1-2", text_all)
        self.assertIn("alpha:src/demo.py:1-2", text_repo)
        self.assertEqual(calls[0][0], None)
        self.assertEqual(calls[1][0], "alpha")

    def test_no_ollama_chat_during_retrieval_tools(self) -> None:
        with patch(
            "local_code_context.retrieval.query.ollama_chat",
            side_effect=AssertionError("ollama_chat should not be called"),
        ):
            fake = FakeCollection(
                [{"repo": "alpha", "repo_root": "/tmp/alpha", "path": "a.py"}]
            )
            config = _config("/tmp/db")
            with patch(
                "local_code_context.mcp.context._open_collection", return_value=fake
            ):
                text = mcp_server._call_tool(config, "list_repositories", {})  # noqa: SLF001
                self.assertIn("Indexed repositories:", text)

            with patch(
                "local_code_context.mcp.context._open_collection", return_value=fake
            ):
                with patch("local_code_context.mcp.context.search_chunks", return_value=[]):
                    text = mcp_server._call_tool(config, "search_code", {"q": "demo"})  # noqa: SLF001
                    self.assertIn("Retrieved sources", text)


if __name__ == "__main__":
    unittest.main()
