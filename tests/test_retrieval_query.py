from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_context.retrieval.query import get_collection  # noqa: E402


class FakeCollection:
    def __init__(self, name: str):
        self.name = name


class FakePersistentClient:
    def __init__(self, path: str):
        self.path = path

    def get_or_create_collection(self, collection_name: str) -> FakeCollection:
        return FakeCollection(collection_name)


class RetrievalQueryTests(unittest.TestCase):
    def test_get_collection_uses_chroma_client(self) -> None:
        fake_chromadb = types.SimpleNamespace(PersistentClient=FakePersistentClient)
        with patch.dict(sys.modules, {"chromadb": fake_chromadb}):
            collection = get_collection(Path("/tmp/db"), "code_chunks")

        self.assertIsInstance(collection, FakeCollection)
        self.assertEqual(collection.name, "code_chunks")


if __name__ == "__main__":
    unittest.main()
