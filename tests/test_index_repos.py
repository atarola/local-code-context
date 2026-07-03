from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_rag import index_repos  # noqa: E402


class FakeCollection:
    def __init__(self) -> None:
        self.add_calls: list[dict[str, object]] = []
        self.deleted: list[list[str]] = []

    def delete(self, ids=None):  # noqa: ANN001
        self.deleted.append(list(ids or []))

    def add(self, **kwargs):  # noqa: ANN001
        self.add_calls.append(kwargs)


class IndexRepoTests(unittest.TestCase):
    def test_index_file_adds_repo_root_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_text("print('hello')\n", encoding="utf-8")
            collection = FakeCollection()
            manifest = {"files": {}}

            with patch(
                "local_code_rag.index_repos.read_text", return_value="print('hello')\n"
            ):
                with patch(
                    "local_code_rag.index_repos.chunk_text",
                    return_value=[(1, 1, "print('hello')")],
                ):
                    with patch(
                        "local_code_rag.index_repos.content_hash", return_value="digest"
                    ):
                        with patch(
                            "local_code_rag.index_repos.ollama_embed",
                            return_value=[[0.1, 0.2]],
                        ):
                            changed = index_repos.index_file(
                                collection=collection,
                                path=path,
                                repo_root=root,
                                repo="demo",
                                db_path=root,
                                manifest=manifest,
                                embed_model="nomic-embed-text",
                                ollama_url="http://localhost:11434",
                                force=False,
                            )

            self.assertTrue(changed)
            self.assertEqual(len(collection.add_calls), 1)
            self.assertEqual(
                collection.add_calls[0]["metadatas"][0]["repo_root"], str(root)
            )
            self.assertEqual(manifest["files"]["demo:demo.py"]["repo_root"], str(root))


if __name__ == "__main__":
    unittest.main()
