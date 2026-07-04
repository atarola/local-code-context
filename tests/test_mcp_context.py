from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

import sys

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_context.mcp import context as mcp_context  # noqa: E402


def _make_db(repo_entries: list[tuple[str, str]]) -> Path:
    tmp = Path(tempfile.mkdtemp())
    db_path = tmp / "xref.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS repo_meta (repo TEXT UNIQUE, root_path TEXT)"
    )
    for repo, root in repo_entries:
        conn.execute(
            "INSERT OR IGNORE INTO repo_meta (repo, root_path) VALUES (?, ?)",
            (repo, root),
        )
    conn.commit()
    conn.close()
    return tmp


class MCPContextTests(unittest.TestCase):
    def test_repository_metadata_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as a_root, tempfile.TemporaryDirectory() as b_root:
            db = _make_db([
                ("alpha", a_root),
                ("beta", b_root),
            ])
            repos = mcp_context.list_indexed_repositories(db)
            self.assertEqual(repos, ["alpha", "beta"])

    def test_missing_repository_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db = _make_db([("alpha", tmpdir)])
            with self.assertRaisesRegex(ValueError, "is not indexed"):
                mcp_context.get_repository_context(db, "beta")

    def test_path_traversal_rejection(self) -> None:
        root = Path("/tmp/example")
        with self.assertRaises(ValueError):
            mcp_context._resolve_repo_path(root, "../secret")

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
            tree_paths = mcp_context._priority_tree_paths(files, root, 20)
            tree = mcp_context._ascii_tree(tree_paths, root)

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
            manifests = mcp_context._manifest_files(root, files)
            names = {path.name for path in manifests}
            self.assertIn("README.md", names)
            self.assertIn("pyproject.toml", names)

    def test_output_truncation(self) -> None:
        text = mcp_context._render_sections(
            [("One", "x" * 2_000), ("Two", "y" * 2_000)], 120
        )
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

            db = _make_db([("demo", str(root))])
            text = mcp_context.get_repository_context(db, "demo", max_chars=10_000)
            self.assertIn("=== Repository ===", text)
            self.assertIn("=== File tree ===", text)
            self.assertIn("=== README ===", text)
            self.assertIn("=== Entry points ===", text)
            self.assertIn("demo:main", text)

    def test_get_workspace_context(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            repo_a = Path(base) / "alpha"
            repo_b = Path(base) / "beta"
            repo_a.mkdir()
            repo_b.mkdir()
            (repo_a / "README.md").write_text("alpha", encoding="utf-8")
            (repo_b / "README.md").write_text("beta", encoding="utf-8")
            db = _make_db([("alpha", str(repo_a)), ("beta", str(repo_b))])
            text = mcp_context.get_workspace_context(db, max_chars_per_repo=2_000)
            self.assertIn("=== Repository ===", text)
            self.assertIn("alpha", text)
            self.assertIn("beta", text)


if __name__ == "__main__":
    unittest.main()
