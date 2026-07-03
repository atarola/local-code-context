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

from local_code_context.indexing import indexer as index_repos  # noqa: E402
from local_code_context.indexing import watcher as watch_repos  # noqa: E402


class MemoryCollection:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}

    def add(self, **kwargs):  # noqa: ANN001
        ids = list(kwargs.get("ids", []))
        documents = list(kwargs.get("documents", []))
        embeddings = list(kwargs.get("embeddings", []))
        metadatas = list(kwargs.get("metadatas", []))
        for index, record_id in enumerate(ids):
            self.records[record_id] = {
                "document": documents[index],
                "embedding": embeddings[index] if index < len(embeddings) else None,
                "metadata": metadatas[index] if index < len(metadatas) else {},
            }

    def delete(self, ids=None, where=None):  # noqa: ANN001
        if ids is not None:
            for record_id in ids:
                self.records.pop(record_id, None)
            return
        if where is None:
            self.records.clear()
            return
        to_delete = [
            record_id
            for record_id, record in self.records.items()
            if all(record["metadata"].get(key) == value for key, value in where.items())
        ]
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


def _record_ids_for_path(
    collection: MemoryCollection, repo: str, rel_path: str
) -> set[str]:
    return {
        record_id
        for record_id, record in collection.records.items()
        if record["metadata"].get("repo") == repo
        and record["metadata"].get("path") == rel_path
    }


class ChangeStub:
    def __init__(self, name: str) -> None:
        self.name = name


def _fake_embeddings(texts, model, base_url):  # noqa: ANN001
    del model, base_url
    return [[float(index) + 0.1] for index, _ in enumerate(texts)]


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

            collection = MemoryCollection()
            manifest = {"files": {}}

            with patch(
                "local_code_context.indexing.indexer.ollama_embed",
                side_effect=_fake_embeddings,
            ):
                index_repos.index_file(
                    collection=collection,
                    path=file_a,
                    repo_root=repo_a,
                    repo="alpha",
                    db_path=base,
                    manifest=manifest,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                    force=False,
                )
                index_repos.index_file(
                    collection=collection,
                    path=file_b,
                    repo_root=repo_b,
                    repo="beta",
                    db_path=base,
                    manifest=manifest,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                    force=False,
                )

            original_beta = _record_ids_for_path(collection, "beta", "b.py")
            file_a.write_text("def a():\n    return 10\n", encoding="utf-8")

            with patch(
                "local_code_context.indexing.indexer.ollama_embed",
                side_effect=_fake_embeddings,
            ):
                counts = watch_repos._process_changes(  # noqa: SLF001
                    changes={(ChangeStub("modified"), str(file_a))},
                    repo_paths=[repo_a, repo_b],
                    collection=collection,
                    manifest=manifest,
                    db_path=base,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                )

            self.assertEqual(counts["indexed"], 1)
            self.assertEqual(
                _record_ids_for_path(collection, "beta", "b.py"), original_beta
            )
            self.assertTrue(_record_ids_for_path(collection, "alpha", "a.py"))

    def test_process_changes_handles_rename_as_delete_and_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "repo"
            root.mkdir()
            old_path = root / "old.py"
            new_path = root / "new.py"
            old_path.write_text("def old():\n    return 1\n", encoding="utf-8")
            new_path.write_text("def new():\n    return 2\n", encoding="utf-8")

            collection = MemoryCollection()
            manifest = {"files": {}}

            with patch(
                "local_code_context.indexing.indexer.ollama_embed",
                side_effect=_fake_embeddings,
            ):
                index_repos.index_file(
                    collection=collection,
                    path=old_path,
                    repo_root=root,
                    repo="repo",
                    db_path=root,
                    manifest=manifest,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                    force=False,
                )

            with patch(
                "local_code_context.indexing.indexer.ollama_embed",
                side_effect=_fake_embeddings,
            ):
                counts = watch_repos._process_changes(  # noqa: SLF001
                    changes={
                        (ChangeStub("deleted"), str(old_path)),
                        (ChangeStub("modified"), str(new_path)),
                    },
                    repo_paths=[root],
                    collection=collection,
                    manifest=manifest,
                    db_path=root,
                    embed_model="nomic-embed-text",
                    ollama_url="http://localhost:11434",
                )

            self.assertEqual(counts["deleted"], 1)
            self.assertEqual(counts["indexed"], 1)
            self.assertFalse(_record_ids_for_path(collection, "repo", "old.py"))
            self.assertTrue(_record_ids_for_path(collection, "repo", "new.py"))


if __name__ == "__main__":
    unittest.main()
