#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from local_code_context.syntax.indexer import INDEX_SCHEMA_VERSION, build_index_records
from local_code_context.syntax.rendering import CHUNK_LINES, CHUNK_OVERLAP, chunk_text


DEFAULT_DB = "./codebase_index"
DEFAULT_COLLECTION = "code_chunks"
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
EMBED_BATCH_SIZE = 32

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".agents",
    ".cache",
    ".codex",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".uv-cache",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "target",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    ".chroma",
    "codebase_index",
}

SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".xz",
    ".7z",
    ".sqlite",
    ".db",
    ".bin",
    ".o",
    ".a",
    ".so",
    ".dylib",
    ".dll",
    ".exe",
    ".pyc",
    ".class",
    ".lock",
}


def repo_name(repo: Path) -> str:
    return repo.resolve().name


def discover_workspace_repos(workspace: Path) -> list[Path]:
    if not workspace.exists() or not workspace.is_dir():
        raise SystemExit(f"workspace does not exist or is not a directory: {workspace}")

    repos: list[Path] = []
    for child in sorted(workspace.iterdir()):
        if not child.is_dir() or should_skip_path(child.relative_to(workspace)):
            continue
        if (child / ".git").exists():
            repos.append(child.resolve())
    return repos


def resolve_repos(
    repos: list[str] | None = None, workspaces: list[str] | None = None
) -> list[Path]:
    repo_paths = [Path(repo).expanduser().resolve() for repo in repos or []]
    for workspace_arg in workspaces or []:
        repo_paths.extend(
            discover_workspace_repos(Path(workspace_arg).expanduser().resolve())
        )

    deduped: dict[str, Path] = {}
    for repo in repo_paths:
        if not repo.exists() or not repo.is_dir():
            raise SystemExit(f"repo does not exist or is not a directory: {repo}")
        deduped[str(repo)] = repo
    return list(deduped.values())


def manifest_path(db_path: Path) -> Path:
    return db_path / "manifest.json"


def open_collection(db_path: Path, collection_name: str) -> Any:
    import chromadb

    client = chromadb.PersistentClient(path=str(db_path))
    return client.get_or_create_collection(collection_name)


def load_manifest(db_path: Path) -> dict[str, Any]:
    path = manifest_path(db_path)
    if not path.exists():
        return {"version": 1, "files": {}}
    with path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest.setdefault("version", 1)
    manifest.setdefault("files", {})
    return manifest


def save_manifest(db_path: Path, manifest: dict[str, Any]) -> None:
    db_path.mkdir(parents=True, exist_ok=True)
    path = manifest_path(db_path)
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(path)


def file_key(repo: str, rel_path: str) -> str:
    return f"{repo}:{rel_path}"


def load_index_ignore(repo: Path) -> list[str]:
    path = repo / ".index_ignore"
    if not path.exists():
        return []

    patterns: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def matches_index_ignore(rel_path: Path, patterns: list[str]) -> bool:
    text = rel_path.as_posix()
    name = rel_path.name
    for pattern in patterns:
        if fnmatch.fnmatchcase(text, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def should_skip_path(path: Path, ignore_patterns: list[str] | None = None) -> bool:
    return (
        any(part in SKIP_DIRS for part in path.parts)
        or any(part.endswith(".egg-info") for part in path.parts)
        or path.name in {".DS_Store", ".gitignore", ".index_ignore"}
        or path.suffix.lower() in SKIP_SUFFIXES
        or any(part.endswith(":Zone.Identifier") for part in path.parts)
        or matches_index_ignore(path, ignore_patterns or [])
    )


def git_list_files(repo: Path) -> list[Path] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard", "-z"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    files: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = Path(raw.decode("utf-8", errors="surrogateescape"))
        if should_skip_path(rel):
            continue
        files.append(repo / rel)
    return sorted(files)


def iter_files(repo: Path) -> list[Path]:
    ignore_patterns = load_index_ignore(repo)
    git_files = git_list_files(repo)
    if git_files is not None:
        return [
            path
            for path in git_files
            if not should_skip_path(path.relative_to(repo), ignore_patterns)
        ]

    files: list[Path] = []
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        if should_skip_path(rel, ignore_patterns):
            continue
        files.append(path)
    return sorted(files)


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError as exc:
        print(f"skip unreadable: {path} ({exc})")
        return None
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return data.decode("latin-1")
        except UnicodeDecodeError:
            return None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ollama_embed(texts: list[str], model: str, base_url: str) -> list[list[float]]:
    import requests

    response = requests.post(
        f"{base_url.rstrip('/')}/api/embed",
        json={"model": model, "input": texts},
        timeout=120,
    )
    response.raise_for_status()
    payload = response.json()
    embeddings = payload.get("embeddings")
    if not isinstance(embeddings, list) or len(embeddings) != len(texts):
        raise RuntimeError(f"unexpected Ollama embed response: {payload}")
    return embeddings


def delete_ids(collection: Any, ids: list[str]) -> None:
    if ids:
        collection.delete(ids=ids)


def _payload_list(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if value is None:
        return []
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], list):
        return list(value[0])
    if isinstance(value, list):
        return list(value)
    return [value]


def _snapshot_records(collection: Any, ids: list[str]) -> dict[str, list[Any]] | None:
    if not ids:
        return None

    try:
        payload = collection.get(
            ids=ids, include=["documents", "embeddings", "metadatas"]
        )
    except Exception as exc:
        print(f"failed to snapshot records for rollback: {exc}")
        return None

    snapshot = {
        "ids": _payload_list(payload, "ids"),
        "documents": _payload_list(payload, "documents"),
        "embeddings": _payload_list(payload, "embeddings"),
        "metadatas": _payload_list(payload, "metadatas"),
    }
    if not snapshot["ids"]:
        return None
    return snapshot


def _restore_records(collection: Any, snapshot: dict[str, list[Any]]) -> None:
    kwargs: dict[str, Any] = {
        "ids": snapshot["ids"],
        "documents": snapshot["documents"],
        "metadatas": snapshot["metadatas"],
    }
    if any(embedding is not None for embedding in snapshot["embeddings"]):
        kwargs["embeddings"] = snapshot["embeddings"]
    collection.add(**kwargs)


def _delete_path_records(
    collection: Any, manifest: dict[str, Any], repo: str, rel_path: str
) -> int:
    key = file_key(repo, rel_path)
    entry = manifest["files"].get(key)
    ids = list(entry.get("ids", [])) if isinstance(entry, dict) else []

    if not ids:
        try:
            payload = collection.get(where={"repo": repo, "path": rel_path})
        except Exception as exc:
            print(f"failed to discover records for deletion: {repo}:{rel_path} ({exc})")
            return 0
        ids = [str(item) for item in _payload_list(payload, "ids")]

    try:
        delete_ids(collection, ids)
    except Exception as exc:
        print(f"failed to delete records for {repo}:{rel_path}: {exc}")
        return 0
    manifest["files"].pop(key, None)
    return len(ids)


def delete_indexed_path(
    collection: Any, manifest: dict[str, Any], repo: str, relative_path: str
) -> int:
    return _delete_path_records(collection, manifest, repo, relative_path)


def index_file(
    collection: Any,
    path: Path,
    repo_root: Path,
    repo: str,
    db_path: Path,
    manifest: dict[str, Any],
    embed_model: str,
    ollama_url: str,
    force: bool,
) -> bool:
    rel_path = path.relative_to(repo_root).as_posix()
    key = file_key(repo, rel_path)
    text = read_text(path)
    if text is None:
        return False
    try:
        source = path.read_bytes()
    except OSError as exc:
        print(f"skip unreadable: {path} ({exc})")
        return False

    digest = content_hash(text)
    existing = manifest["files"].get(key)
    if not force and existing and existing.get("hash") == digest:
        return False

    build_result = build_index_records(
        repo=repo,
        repo_root=repo_root,
        path=path,
        source=source,
        text=text,
    )
    records = build_result.records
    if not records:
        _delete_path_records(collection, manifest, repo, rel_path)
        return True

    ids: list[str] = [record.id for record in records]
    documents: list[str] = [record.document for record in records]
    metadatas: list[dict[str, Any]] = [record.metadata for record in records]

    embeddings: list[list[float]] = []
    for start in range(0, len(documents), EMBED_BATCH_SIZE):
        batch = documents[start : start + EMBED_BATCH_SIZE]
        batch_embeddings = ollama_embed(batch, embed_model, ollama_url)
        if len(batch_embeddings) != len(batch):
            raise RuntimeError("embedding count did not match document count")
        embeddings.extend(batch_embeddings)

    existing_ids = list(existing.get("ids", [])) if isinstance(existing, dict) else []
    backup = _snapshot_records(collection, existing_ids)

    if existing_ids:
        try:
            delete_ids(collection, existing_ids)
        except Exception as exc:
            print(f"failed to delete previous records for {path}: {exc}")
            return False

    try:
        collection.add(
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
    except Exception as exc:
        print(f"failed to index {path}: {exc}")
        if backup is not None:
            try:
                _restore_records(collection, backup)
            except Exception as restore_exc:
                print(f"failed to restore previous records for {path}: {restore_exc}")
        return False

    manifest["files"][key] = {
        "repo": repo,
        "repo_root": str(repo_root),
        "path": rel_path,
        "hash": digest,
        "ids": ids,
        "chunk_lines": CHUNK_LINES,
        "chunk_overlap": CHUNK_OVERLAP,
        "embed_model": embed_model,
        "index_schema_version": INDEX_SCHEMA_VERSION,
    }
    return True


def prune_deleted(
    collection: Any, manifest: dict[str, Any], repo: str, seen_keys: set[str]
) -> int:
    removed = 0
    for key, entry in list(manifest["files"].items()):
        if entry.get("repo") != repo or key in seen_keys:
            continue
        rel_path = entry.get("path")
        if isinstance(rel_path, str) and _delete_path_records(
            collection, manifest, repo, rel_path
        ):
            removed += 1
    return removed


def run_index(
    repos: list[str],
    db: str = DEFAULT_DB,
    collection_name: str = DEFAULT_COLLECTION,
    embed_model: str = DEFAULT_EMBED_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    force: bool = False,
    workspaces: list[str] | None = None,
) -> None:
    import chromadb

    repo_paths = resolve_repos(repos=repos, workspaces=workspaces)
    if not repo_paths:
        raise SystemExit("at least one --repo or --workspace is required")

    db_path = Path(db).expanduser().resolve()
    client = chromadb.PersistentClient(path=str(db_path))
    collection = client.get_or_create_collection(collection_name)
    manifest = load_manifest(db_path)

    total_changed = 0
    total_skipped = 0
    total_deleted = 0

    for repo_root in repo_paths:
        repo = repo_name(repo_root)
        files = iter_files(repo_root)
        seen_keys = {
            file_key(repo, path.relative_to(repo_root).as_posix()) for path in files
        }

        changed = 0
        skipped = 0
        for path in files:
            did_change = index_file(
                collection=collection,
                path=path,
                repo_root=repo_root,
                repo=repo,
                db_path=db_path,
                manifest=manifest,
                embed_model=embed_model,
                ollama_url=ollama_url,
                force=force,
            )
            if did_change:
                changed += 1
            else:
                skipped += 1

        deleted = prune_deleted(collection, manifest, repo, seen_keys)
        total_changed += changed
        total_skipped += skipped
        total_deleted += deleted
        print(
            f"{repo}: indexed/updated {changed}, skipped {skipped}, removed {deleted}"
        )

    save_manifest(db_path, manifest)
    print(
        f"done: indexed/updated {total_changed}, skipped {total_skipped}, removed {total_deleted}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index one or more repos into a local Chroma DB."
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repo path to index. Repeat for multiple repos.",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        help="Workspace directory whose immediate Git child directories should be indexed. Repeat for multiple workspaces.",
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"Chroma DB directory. Default: {DEFAULT_DB}"
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        help=f"Chroma collection. Default: {DEFAULT_COLLECTION}",
    )
    parser.add_argument(
        "--embed-model",
        default=DEFAULT_EMBED_MODEL,
        help=f"Ollama embedding model. Default: {DEFAULT_EMBED_MODEL}",
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_URL,
        help=f"Ollama base URL. Default: {DEFAULT_OLLAMA_URL}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-embed every file, ignoring the manifest hash cache.",
    )
    args = parser.parse_args()

    run_index(
        repos=args.repo,
        workspaces=args.workspace,
        db=args.db,
        collection_name=args.collection,
        embed_model=args.embed_model,
        ollama_url=args.ollama_url,
        force=args.force,
    )


if __name__ == "__main__":
    main()
