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
from local_code_context.indexing import watcher as watch_repos  # noqa: E402
from local_code_context.storage.schema import get_db_path, open_db  # noqa: E402


class ChangeStub:
    def __init__(self, name: str) -> None:
        self.name = name


class WatchRepoTests(unittest.TestCase):
    def test_process_changes_updates_only_one_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            repo_a = base / "alpha"
            repo_b = base / "beta"
            repo_a.mkdir()
            repo_b.mkdir()
            file_a = repo_a / "a.py"
            file_b = repo_b / "b.py"
            file_a.write_text("def a():\n    return 1\n", encoding="utf-8")
            file_b.write_text("def b():\n    return 2\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            index_repos.index_file(
                path=file_a,
                repo_root=repo_a,
                repo="alpha",
                db_path=base,
                manifest=manifest,
            )
            index_repos.index_file(
                path=file_b,
                repo_root=repo_b,
                repo="beta",
                db_path=base,
                manifest=manifest,
            )

            xref_db = get_db_path(base)
            conn = open_db(xref_db)
            beta_before = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM symbols WHERE repo = 'beta' AND path = 'b.py'"
                ).fetchall()
            }
            conn.close()
            self.assertIn("b", beta_before)

            file_a.write_text("def a2():\n    return 10\n", encoding="utf-8")

            counts = watch_repos._process_changes(
                changes={(ChangeStub("modified"), str(file_a))},
                repo_paths=[repo_a, repo_b],
                manifest=manifest,
                db_path=base,
            )

            self.assertEqual(counts["indexed"], 1)

            conn = open_db(xref_db)
            beta_after = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM symbols WHERE repo = 'beta' AND path = 'b.py'"
                ).fetchall()
            }
            conn.close()
            self.assertEqual(beta_after, beta_before)

    def test_process_changes_handles_rename_as_delete_and_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            root.mkdir()
            old_path = root / "old.py"
            new_path = root / "new.py"
            old_path.write_text("def old():\n    return 1\n", encoding="utf-8")
            new_path.write_text("def new():\n    return 2\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            index_repos.index_file(
                path=old_path,
                repo_root=root,
                repo="repo",
                db_path=root,
                manifest=manifest,
            )

            xref_db = get_db_path(root)
            conn = open_db(xref_db)
            old_before = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM symbols WHERE repo = 'repo' AND path = 'old.py'"
                ).fetchall()
            }
            conn.close()
            self.assertIn("old", old_before)

            counts = watch_repos._process_changes(
                changes={
                    (ChangeStub("deleted"), str(old_path)),
                    (ChangeStub("modified"), str(new_path)),
                },
                repo_paths=[root],
                manifest=manifest,
                db_path=root,
            )

            self.assertEqual(counts["deleted"], 1)
            self.assertEqual(counts["indexed"], 1)

            conn = open_db(xref_db)
            old_after = conn.execute(
                "SELECT COUNT(*) as cnt FROM symbols WHERE repo = 'repo' AND path = 'old.py'"
            ).fetchone()
            new_after = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM symbols WHERE repo = 'repo' AND path = 'new.py'"
                ).fetchall()
            }
            conn.close()
            self.assertEqual(old_after["cnt"], 0)
            self.assertIn("new", new_after)


if __name__ == "__main__":
    unittest.main()
