from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_context.indexing import indexer as index_repos  # noqa: E402
from local_code_context.storage.schema import get_db_path, open_db  # noqa: E402
from local_code_context.storage.writer import delete_file_xref  # noqa: E402


class IndexRepoTests(unittest.TestCase):
    def test_index_file_writes_to_xref(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_text("def hello():\n    return 42\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            changed = index_repos.index_file(
                path=path,
                repo_root=root,
                repo="demo",
                db_path=root,
                manifest=manifest,
            )

            self.assertTrue(changed)
            self.assertIn("demo:demo.py", manifest)

            xref_db = get_db_path(root)
            conn = open_db(xref_db)
            rows = conn.execute(
                "SELECT name, kind FROM symbols WHERE repo = ? AND path = ?",
                ("demo", "demo.py"),
            ).fetchall()
            conn.close()
            names = {r["name"]: r["kind"] for r in rows}
            self.assertIn("hello", names)

    def test_index_file_skips_unchanged_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_text("def hello():\n    return 42\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            first = index_repos.index_file(
                path=path,
                repo_root=root,
                repo="demo",
                db_path=root,
                manifest=manifest,
            )
            self.assertTrue(first)

            second = index_repos.index_file(
                path=path,
                repo_root=root,
                repo="demo",
                db_path=root,
                manifest=manifest,
            )
            self.assertFalse(second)

    def test_delete_file_xref_removes_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_text("def hello():\n    return 42\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            index_repos.index_file(
                path=path,
                repo_root=root,
                repo="demo",
                db_path=root,
                manifest=manifest,
            )

            delete_file_xref(root, "demo", "demo.py")

            xref_db = get_db_path(root)
            conn = open_db(xref_db)
            rows = conn.execute(
                "SELECT COUNT(*) as cnt FROM symbols WHERE repo = ? AND path = ?",
                ("demo", "demo.py"),
            ).fetchall()
            conn.close()
            self.assertEqual(rows[0]["cnt"], 0)


if __name__ == "__main__":
    unittest.main()
