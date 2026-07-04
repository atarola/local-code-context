from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_context.indexing import indexer as index_repos
from local_code_context.indexing import watcher as watch_repos
from local_code_context.storage.schema import get_db_path, open_db
from local_code_context.storage.writer import index_file_xref, delete_file_xref
from local_code_context.storage.resolver import (
    resolve_repo_relationships,
    resolve_imports_for_repo,
    resolve_call_sites_for_repo,
)
from local_code_context.syntax.models import (
    CodeCall,
    CodeImport,
    CodeSymbol,
    ExtractionResult,
)


def _create_repo(tmpdir: str, name: str = "repo") -> Path:
    root = Path(tmpdir) / name
    root.mkdir()
    return root


def _integrity_check(db_path: Path) -> None:
    xref_db = get_db_path(db_path)
    conn = open_db(xref_db)
    try:
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        if fk:
            raise AssertionError(f"Foreign key violations: {fk}")
        if integrity is None or integrity[0] != "ok":
            raise AssertionError(f"Integrity check failed: {integrity}")
    except Exception:
        conn.close()
        raise


def _normalized_tables(db_path: Path) -> dict[str, list[dict]]:
    xref_db = get_db_path(db_path)
    conn = open_db(xref_db)
    try:
        result: dict[str, list[dict]] = {}
        tables = [
            ("repo_meta", "repo"),
            ("symbols", "repo, path, name, kind, start_line, parent, exported, signature, language"),
            ("imports", "repo, path, source_module, imported_name, start_line"),
            ("resolved_imports", "import_id, symbol_id"),
            ("call_sites", "repo, path, callee_name, callee_qualifier, start_line, resolution_status, language"),
            ("file_vibe", "repo, path, summary"),
        ]
        for table, order_cols in tables:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY {order_cols}"
            ).fetchall()
            result[table] = [dict(r) for r in rows]
        return result
    finally:
        conn.close()


class ChangeStub:
    def __init__(self, name: str) -> None:
        self.name = name


class IntegrationTests(unittest.TestCase):
    """A. Batch deletion removes all path records"""

    def test_batch_deletion_removes_all_path_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            path = root / "demo.py"
            path.write_text(
                "import os\n\ndef old_function():\n    helper()\n\ndef helper():\n    pass\n",
                encoding="utf-8",
            )

            repo = index_repos.repo_name(root)

            # Index via run_index to properly persist manifest
            index_repos.run_index(repos=[str(root)], db=str(db_path))

            manifest = index_repos.load_manifest(db_path)

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            sym_count = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            imp_count = conn.execute(
                "SELECT COUNT(*) FROM imports WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            conn.close()
            self.assertGreater(sym_count, 0)
            self.assertGreater(imp_count, 0)

            # Delete file from disk
            path.unlink()

            # Run batch indexer
            index_repos.run_index(repos=[str(root)], db=str(db_path))

            manifest = index_repos.load_manifest(db_path)
            self.assertNotIn(f"{repo}:demo.py", manifest)

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            for table in ("symbols", "imports"):
                cnt = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE repo=? AND path=?",
                    (repo, "demo.py"),
                ).fetchone()[0]
                self.assertEqual(cnt, 0, f"{table} should be empty for deleted file")
            call_cnt = conn.execute(
                "SELECT COUNT(*) FROM call_sites WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            self.assertEqual(call_cnt, 0)
            vibe_cnt = conn.execute(
                "SELECT COUNT(*) FROM file_vibe WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            self.assertEqual(vibe_cnt, 0)
            imp_res_cnt = conn.execute(
                "SELECT COUNT(*) FROM resolved_imports "
                "WHERE import_id IN (SELECT id FROM imports WHERE repo=? AND path=?)",
                (repo, "demo.py"),
            ).fetchone()[0]
            self.assertEqual(imp_res_cnt, 0)
            conn.close()

            _integrity_check(db_path)

    """B. Empty-file replacement"""

    def test_empty_file_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            path = root / "demo.py"
            path.write_text("def foo():\n    return 1\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            repo = index_repos.repo_name(root)

            index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            self.assertIn(f"{repo}:demo.py", manifest)

            # Replace with empty file
            path.write_text("", encoding="utf-8")
            old_key = f"{repo}:demo.py"
            old_hash = manifest.get(old_key)

            result = index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            self.assertIsNotNone(result, "empty file should not fail")
            self.assertTrue(result, "empty file should be indexed")

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            sym_cnt = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(sym_cnt, 0, "empty file should have no symbols")

            new_hash = manifest.get(old_key)
            self.assertIsNotNone(new_hash)
            self.assertNotEqual(new_hash, old_hash)

            # Re-run and confirm skipped
            result2 = index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            self.assertFalse(result2, "unchanged empty file should be skipped")

            _integrity_check(db_path)

    """C. Comment-only replacement"""

    def test_comment_only_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            path = root / "demo.py"
            path.write_text("def foo():\n    return 1\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            repo = index_repos.repo_name(root)

            index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            old_syms = conn.execute(
                "SELECT name FROM symbols WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchall()
            conn.close()
            self.assertGreater(len(old_syms), 0)

            # Replace with comment-only
            path.write_text("# just a comment\n", encoding="utf-8")

            result = index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            self.assertTrue(result, "comment-only file should be indexed")

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            sym_cnt = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(sym_cnt, 0, "comment-only file should have no symbols")

            _integrity_check(db_path)

    """D. Import-only replacement"""

    def test_import_only_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            path = root / "demo.py"
            path.write_text(
                "def foo():\n    return 1\n\nimport os\n",
                encoding="utf-8",
            )

            manifest: dict[str, str] = {}
            repo = index_repos.repo_name(root)

            index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            old_syms = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            old_imps = conn.execute(
                "SELECT COUNT(*) FROM imports WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            conn.close()
            self.assertGreater(old_syms, 0)
            self.assertGreater(old_imps, 0)

            # Replace with imports only (no symbols)
            path.write_text("import sys\nimport os\n", encoding="utf-8")

            result = index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            self.assertTrue(result, "import-only file should be indexed")

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            sym_cnt = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            imp_cnt = conn.execute(
                "SELECT COUNT(*) FROM imports WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(sym_cnt, 0, "import-only file should have no symbols")
            self.assertGreater(imp_cnt, 0, "import-only file should preserve imports")

            _integrity_check(db_path)

    """E. Extraction failure preserves prior state"""

    def test_extraction_failure_preserves_prior_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            path = root / "main.unknown"
            path.write_text("def foo():\n    return 1\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            repo = index_repos.repo_name(root)

            result = index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            self.assertIsNone(result, "unknown extension should fail")

            _integrity_check(db_path)

    """F. Rename equivalence"""

    def test_rename_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            old_path = root / "old.py"
            new_path = root / "new.py"
            old_path.write_text("def foo():\n    return 1\n", encoding="utf-8")

            repo = index_repos.repo_name(root)

            # Use run_index so manifest is persisted
            index_repos.run_index(repos=[str(root)], db=str(db_path))
            manifest = index_repos.load_manifest(db_path)
            self.assertIn(f"{repo}:old.py", manifest)

            # Rename: remove old file, create new file
            old_path.unlink()
            new_path.write_text("def foo():\n    return 1\n", encoding="utf-8")

            index_repos.run_index(repos=[str(root)], db=str(db_path))
            manifest = index_repos.load_manifest(db_path)

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            old_cnt = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE repo=? AND path=?",
                (repo, "old.py"),
            ).fetchone()[0]
            new_cnt = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE repo=? AND path=?",
                (repo, "new.py"),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(old_cnt, 0, "old file symbols should be removed")
            self.assertGreater(new_cnt, 0, "new file symbols should be present")

            self.assertNotIn(f"{repo}:old.py", manifest)
            self.assertIn(f"{repo}:new.py", manifest)

            _integrity_check(db_path)

    """G. Watcher startup reconciliation"""

    def test_watcher_startup_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            path = root / "demo.py"
            path.write_text("def foo():\n    return 1\n", encoding="utf-8")

            manifest: dict[str, str] = {}
            repo = index_repos.repo_name(root)

            index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            self.assertIn(f"{repo}:demo.py", manifest)

            # Simulate: delete file, then do watcher startup reconciliation
            path.unlink()

            stale = watch_repos._stale_keys_for(manifest, root)
            self.assertEqual(len(stale), 1)

            for key in stale:
                rel_path = index_repos.parse_file_key(key, repo)
                self.assertIsNotNone(rel_path)
                delete_file_xref(db_path, repo, rel_path)
                del manifest[key]
            resolve_repo_relationships(db_path, repo)

            self.assertNotIn(f"{repo}:demo.py", manifest)

            xref_db = get_db_path(db_path)
            conn = open_db(xref_db)
            sym_cnt = conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE repo=? AND path=?",
                (repo, "demo.py"),
            ).fetchone()[0]
            conn.close()
            self.assertEqual(sym_cnt, 0)

            _integrity_check(db_path)

    """H. Clean-build versus incremental equivalence"""

    def test_clean_build_versus_incremental_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            clean_db = Path(tmpdir) / "clean"
            seq_db = Path(tmpdir) / "incremental"
            root = _create_repo(tmpdir)

            files = {
                "alpha.py": "def existing():\n    return 1\n",
                "beta.py": "from alpha import existing\n\ndef use():\n    return existing()\n",
                "gamma.py": "class Holder:\n    def method(self):\n        pass\n",
                "delta.py": "# import-only file\nimport os\n",
                "epsilon.py": "",
            }
            for name, content in files.items():
                (root / name).write_text(content, encoding="utf-8")

            repo = index_repos.repo_name(root)
            manifest: dict[str, str] = {}

            # 1. Initial index all files
            for name in sorted(files):
                p = root / name
                index_repos.index_file(
                    path=p, repo_root=root, repo=repo,
                    db_path=seq_db, manifest=manifest,
                )

            # 2. Modify a file (add a new function)
            (root / "alpha.py").write_text(
                "def existing():\n    return 2\n\ndef new_func():\n    pass\n",
                encoding="utf-8",
            )
            index_repos.index_file(
                path=root / "alpha.py", repo_root=root, repo=repo,
                db_path=seq_db, manifest=manifest,
            )

            # 3. Delete delta.py
            (root / "delta.py").unlink()
            for key in sorted(manifest):
                rel_path = index_repos.parse_file_key(key, repo)
                if rel_path == "delta.py":
                    delete_file_xref(seq_db, repo, rel_path)
                    del manifest[key]

            # 4. Rename gamma.py -> gamma_renamed.py
            (root / "gamma.py").unlink()
            for key in sorted(manifest):
                rel_path = index_repos.parse_file_key(key, repo)
                if rel_path == "gamma.py":
                    delete_file_xref(seq_db, repo, rel_path)
                    del manifest[key]
            (root / "gamma_renamed.py").write_text(
                "class Holder:\n    def method(self):\n        pass\n",
                encoding="utf-8",
            )
            index_repos.index_file(
                path=root / "gamma_renamed.py", repo_root=root, repo=repo,
                db_path=seq_db, manifest=manifest,
            )

            # 5. Empty beta.py
            (root / "beta.py").write_text("", encoding="utf-8")
            index_repos.index_file(
                path=root / "beta.py", repo_root=root, repo=repo,
                db_path=seq_db, manifest=manifest,
            )

            # 6. Populate epsilon.py with a definition
            (root / "epsilon.py").write_text(
                "def helper():\n    pass\n", encoding="utf-8"
            )
            index_repos.index_file(
                path=root / "epsilon.py", repo_root=root, repo=repo,
                db_path=seq_db, manifest=manifest,
            )

            # 7. Remove new_func from alpha.py
            (root / "alpha.py").write_text(
                "def existing():\n    return 2\n",
                encoding="utf-8",
            )
            index_repos.index_file(
                path=root / "alpha.py", repo_root=root, repo=repo,
                db_path=seq_db, manifest=manifest,
            )

            resolve_repo_relationships(seq_db, repo)

            # Build clean DB from final filesystem state
            final_files = {"alpha.py", "beta.py", "gamma_renamed.py", "epsilon.py"}
            c_manifest: dict[str, str] = {}
            for name in sorted(final_files):
                p = root / name
                if p.exists():
                    index_repos.index_file(
                        path=p, repo_root=root, repo=repo,
                        db_path=clean_db, manifest=c_manifest,
                    )
            resolve_repo_relationships(clean_db, repo)

            # Compare normalized tables (excluding auto-increment IDs)
            inc = _normalized_tables(seq_db)
            cln = _normalized_tables(clean_db)

            id_excluded = ("id", "caller_symbol_id", "resolved_symbol_id", "import_id", "symbol_id")

            for table in ("symbols", "imports", "resolved_imports", "call_sites", "file_vibe"):
                inc_rows = [{k: v for k, v in r.items() if k not in id_excluded} for r in inc[table]]
                cln_rows = [{k: v for k, v in r.items() if k not in id_excluded} for r in cln[table]]
                self.assertEqual(
                    inc_rows, cln_rows,
                    f"Mismatch in {table}. "
                    f"Incremental: {inc_rows}, Clean: {cln_rows}",
                )

            _integrity_check(seq_db)
            _integrity_check(clean_db)

    """I. Watcher versus clean-build equivalence"""

    def test_watcher_versus_clean_build_equivalence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            watcher_db = Path(tmpdir) / "watcher"
            clean_db = Path(tmpdir) / "clean"
            root = _create_repo(tmpdir)

            files = {
                "alpha.py": "def existing():\n    return 1\n",
                "beta.py": "from alpha import existing\n\ndef use():\n    return existing()\n",
            }
            for name, content in sorted(files.items()):
                (root / name).write_text(content, encoding="utf-8")

            repo = index_repos.repo_name(root)

            # Build watcher DB via _process_changes and stale reconciliation
            w_manifest: dict[str, str] = {}
            w_changes: set[tuple[object, str]] = set()
            for name in sorted(files):
                w_changes.add((ChangeStub("modified"), str(root / name)))
            watch_repos._process_changes(
                changes=w_changes,
                repo_paths=[root],
                manifest=w_manifest,
                db_path=watcher_db,
            )

            (root / "alpha.py").write_text(
                "def existing():\n    return 2\n\ndef new_func():\n    pass\n",
                encoding="utf-8",
            )
            watch_repos._process_changes(
                changes={(ChangeStub("modified"), str(root / "alpha.py"))},
                repo_paths=[root],
                manifest=w_manifest,
                db_path=watcher_db,
            )

            (root / "beta.py").unlink()
            for key in sorted(w_manifest):
                rel_path = index_repos.parse_file_key(key, repo)
                if rel_path == "beta.py":
                    delete_file_xref(watcher_db, repo, rel_path)
                    del w_manifest[key]

            resolve_repo_relationships(watcher_db, repo)

            # Build clean DB from final state
            c_manifest: dict[str, str] = {}
            for name in sorted({"alpha.py"}):
                p = root / name
                if p.exists():
                    index_repos.index_file(
                        path=p, repo_root=root, repo=repo,
                        db_path=clean_db, manifest=c_manifest,
                    )
            resolve_repo_relationships(clean_db, repo)

            id_excluded = ("id", "caller_symbol_id", "resolved_symbol_id", "import_id", "symbol_id")

            watcher_tables = _normalized_tables(watcher_db)
            clean_tables = _normalized_tables(clean_db)

            for table in ("symbols", "imports", "resolved_imports", "call_sites", "file_vibe"):
                watcher_rows = [{k: v for k, v in r.items() if k not in id_excluded} for r in watcher_tables[table]]
                clean_rows = [{k: v for k, v in r.items() if k not in id_excluded} for r in clean_tables[table]]
                self.assertEqual(
                    watcher_rows, clean_rows,
                    f"Mismatch in {table}",
                )

            _integrity_check(watcher_db)
            _integrity_check(clean_db)

    """J. Resolution idempotency"""

    def test_resolution_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            sym = CodeSymbol(
                name="helper", kind="function", language="python",
                path="mod.py", start_line=1, end_line=3,
                start_byte=0, end_byte=30, signature="def helper()",
                parent=None, exported=True,
            )
            imp = CodeImport(
                source="mod", imported_names=("helper",), path="main.py", start_line=1,
            )
            call = CodeCall(
                caller_name="foo", callee_name="helper", path="main.py",
                start_line=3, callee_qualifier=None,
                caller_symbol_key=None,
            )

            index_file_xref(
                db_path=db_path, repo="test", path="mod.py",
                extraction=ExtractionResult(symbols=[sym], imports=[]),
            )
            index_file_xref(
                db_path=db_path, repo="test", path="main.py",
                symbols=[], imports=[imp], calls=[call],
            )

            # First resolution
            stats1 = resolve_repo_relationships(db_path, "test")

            # Get the state after first resolution
            state1 = _normalized_tables(db_path)

            # Second resolution - must be idempotent
            stats2 = resolve_repo_relationships(db_path, "test")

            state2 = _normalized_tables(db_path)

            # Same logical rows
            for table in ("resolved_imports", "call_sites", "imports", "symbols"):
                self.assertEqual(state1[table], state2[table], f"Mismatch in {table}")

            # Same counts
            self.assertEqual(stats1["resolved"], stats2["resolved"])
            self.assertEqual(stats1["ambiguous"], stats2["ambiguous"])
            self.assertEqual(stats1["unresolved"], stats2["unresolved"])

            _integrity_check(db_path)

    """K. Database integrity after scenarios"""

    def test_integrity_after_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            path = root / "demo.py"
            path.write_text(
                "import os\n\ndef foo():\n    helper()\n\ndef helper():\n    pass\n",
                encoding="utf-8",
            )

            manifest: dict[str, str] = {}
            repo = index_repos.repo_name(root)

            index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            resolve_repo_relationships(db_path, repo)

            delete_file_xref(db_path, repo, "demo.py")
            _integrity_check(db_path)

    def test_integrity_after_empty_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            path = root / "demo.py"
            path.write_text("def foo():\n    return 1\n", encoding="utf-8")

            repo = index_repos.repo_name(root)
            manifest: dict[str, str] = {}

            index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            resolve_repo_relationships(db_path, repo)

            path.write_text("", encoding="utf-8")
            index_repos.index_file(
                path=path, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            _integrity_check(db_path)

    def test_integrity_after_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir)
            root = _create_repo(tmpdir)

            old = root / "old.py"
            new = root / "new.py"
            old.write_text("def foo():\n    return 1\n", encoding="utf-8")

            repo = index_repos.repo_name(root)
            manifest: dict[str, str] = {}

            index_repos.index_file(
                path=old, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            resolve_repo_relationships(db_path, repo)

            old.unlink()
            new.write_text("def foo():\n    return 1\n", encoding="utf-8")
            index_repos.index_file(
                path=new, repo_root=root, repo=repo,
                db_path=db_path, manifest=manifest,
            )
            resolve_repo_relationships(db_path, repo)
            _integrity_check(db_path)


if __name__ == "__main__":
    unittest.main()
