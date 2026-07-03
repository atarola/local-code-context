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


class MemoryCollection:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.add_calls: list[dict[str, object]] = []
        self.delete_calls: list[dict[str, object]] = []

    def add(self, **kwargs):  # noqa: ANN001
        ids = list(kwargs.get("ids", []))
        documents = list(kwargs.get("documents", []))
        embeddings = list(kwargs.get("embeddings", []))
        metadatas = list(kwargs.get("metadatas", []))
        self.add_calls.append(kwargs)
        for index, record_id in enumerate(ids):
            self.records[record_id] = {
                "document": documents[index],
                "embedding": embeddings[index] if index < len(embeddings) else None,
                "metadata": metadatas[index] if index < len(metadatas) else {},
            }

    def delete(self, ids=None, where=None):  # noqa: ANN001
        self.delete_calls.append({"ids": list(ids or []), "where": where})
        if ids is not None:
            for record_id in ids:
                self.records.pop(record_id, None)
            return
        if where is None:
            self.records.clear()
            return
        to_delete = []
        for record_id, record in self.records.items():
            metadata = record["metadata"]
            if all(metadata.get(key) == value for key, value in where.items()):
                to_delete.append(record_id)
        for record_id in to_delete:
            self.records.pop(record_id, None)

    def get(self, ids=None, where=None, include=None):  # noqa: ANN001
        include = include or []
        if ids is not None:
            selected = [(record_id, self.records.get(record_id)) for record_id in ids]
        elif where is not None:
            selected = [
                (record_id, record)
                for record_id, record in self.records.items()
                if all(
                    record["metadata"].get(key) == value for key, value in where.items()
                )
            ]
        else:
            selected = list(self.records.items())

        response: dict[str, list[object]] = {
            "ids": [record_id for record_id, record in selected if record is not None],
        }
        if "documents" in include:
            response["documents"] = [
                record["document"]
                for record_id, record in selected
                if record is not None
            ]
        if "embeddings" in include:
            response["embeddings"] = [
                record["embedding"]
                for record_id, record in selected
                if record is not None
            ]
        if "metadatas" in include:
            response["metadatas"] = [
                record["metadata"]
                for record_id, record in selected
                if record is not None
            ]
        return response


class FlakyMemoryCollection(MemoryCollection):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next_add = False

    def add(self, **kwargs):  # noqa: ANN001
        if self.fail_next_add:
            self.fail_next_add = False
            raise RuntimeError("boom")
        super().add(**kwargs)


def _fake_embeddings(texts, model, base_url):  # noqa: ANN001
    del model, base_url
    return [[float(index) + 0.1] for index, _ in enumerate(texts)]


def _record_ids_for_path(
    collection: MemoryCollection, repo: str, rel_path: str
) -> set[str]:
    return {
        record_id
        for record_id, record in collection.records.items()
        if record["metadata"].get("repo") == repo
        and record["metadata"].get("path") == rel_path
    }


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

    def test_index_file_replaces_only_one_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("def first():\n    return 1\n", encoding="utf-8")
            second.write_text("def second():\n    return 2\n", encoding="utf-8")

            collection = MemoryCollection()
            manifest = {"files": {}}

            with patch(
                "local_code_rag.index_repos.ollama_embed",
                side_effect=lambda texts, model, base_url: [
                    [float(i)] for i, _ in enumerate(texts)
                ],
            ):
                index_repos.index_file(
                    collection=collection,
                    path=first,
                    repo_root=root,
                    repo="demo",
                    db_path=root,
                    manifest=manifest,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                    force=False,
                )
                index_repos.index_file(
                    collection=collection,
                    path=second,
                    repo_root=root,
                    repo="demo",
                    db_path=root,
                    manifest=manifest,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                    force=False,
                )

            before_second = _record_ids_for_path(collection, "demo", "second.py")
            self.assertTrue(before_second)
            original_first_ids = _record_ids_for_path(collection, "demo", "first.py")
            self.assertTrue(original_first_ids)
            original_first_documents = {
                record_id: collection.records[record_id]["document"]
                for record_id in original_first_ids
            }

            first.write_text("def first():\n    return 10\n", encoding="utf-8")
            with patch(
                "local_code_rag.index_repos.ollama_embed",
                side_effect=lambda texts, model, base_url: [
                    [float(i) + 10.0] for i, _ in enumerate(texts)
                ],
            ):
                changed = index_repos.index_file(
                    collection=collection,
                    path=first,
                    repo_root=root,
                    repo="demo",
                    db_path=root,
                    manifest=manifest,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                    force=False,
                )

            self.assertTrue(changed)
            self.assertEqual(
                _record_ids_for_path(collection, "demo", "second.py"), before_second
            )
            current_first_ids = _record_ids_for_path(collection, "demo", "first.py")
            self.assertEqual(current_first_ids, original_first_ids)
            self.assertTrue(
                any(
                    collection.records[record_id]["document"]
                    != original_first_documents[record_id]
                    for record_id in current_first_ids
                )
            )

    def test_index_file_restores_previous_records_on_add_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            path = root / "demo.py"
            path.write_text("def demo():\n    return 1\n", encoding="utf-8")
            collection = FlakyMemoryCollection()
            manifest = {"files": {}}

            with patch(
                "local_code_rag.index_repos.ollama_embed",
                side_effect=_fake_embeddings,
            ):
                index_repos.index_file(
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

            original_ids = _record_ids_for_path(collection, "demo", "demo.py")
            self.assertTrue(original_ids)
            original_hash = manifest["files"]["demo:demo.py"]["hash"]

            path.write_text("def demo():\n    return 2\n", encoding="utf-8")
            collection.fail_next_add = True
            with patch(
                "local_code_rag.index_repos.ollama_embed",
                side_effect=_fake_embeddings,
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

            self.assertFalse(changed)
            self.assertEqual(
                _record_ids_for_path(collection, "demo", "demo.py"), original_ids
            )
            self.assertEqual(manifest["files"]["demo:demo.py"]["hash"], original_hash)

    def test_delete_indexed_path_removes_only_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            first = root / "first.py"
            second = root / "second.py"
            first.write_text("def first():\n    return 1\n", encoding="utf-8")
            second.write_text("def second():\n    return 2\n", encoding="utf-8")

            collection = MemoryCollection()
            manifest = {"files": {}}

            with patch(
                "local_code_rag.index_repos.ollama_embed",
                side_effect=_fake_embeddings,
            ):
                index_repos.index_file(
                    collection=collection,
                    path=first,
                    repo_root=root,
                    repo="demo",
                    db_path=root,
                    manifest=manifest,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                    force=False,
                )
                index_repos.index_file(
                    collection=collection,
                    path=second,
                    repo_root=root,
                    repo="demo",
                    db_path=root,
                    manifest=manifest,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                    force=False,
                )

            removed = index_repos.delete_indexed_path(
                collection, manifest, "demo", "first.py"
            )
            self.assertGreater(removed, 0)
            self.assertFalse(_record_ids_for_path(collection, "demo", "first.py"))
            self.assertTrue(_record_ids_for_path(collection, "demo", "second.py"))
            self.assertNotIn("demo:first.py", manifest["files"])


if __name__ == "__main__":
    unittest.main()
