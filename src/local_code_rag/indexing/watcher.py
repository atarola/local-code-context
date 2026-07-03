#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from local_code_rag.indexing.indexer import (
    DEFAULT_COLLECTION,
    DEFAULT_DB,
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_URL,
    delete_indexed_path,
    index_file,
    load_manifest,
    open_collection,
    repo_name,
    resolve_repos,
    save_manifest,
    run_index,
)


def _owner_repo(path: Path, repos: list[Path]) -> tuple[Path, str] | None:
    for repo in repos:
        try:
            rel = path.relative_to(repo)
        except ValueError:
            continue
        return repo, rel.as_posix()
    return None


def _change_name(change: Any) -> str:
    name = getattr(change, "name", None)
    if isinstance(name, str):
        return name
    return str(change)


def _process_changes(
    *,
    changes: set[tuple[Any, str]],
    repo_paths: list[Path],
    collection: Any,
    manifest: dict[str, Any],
    db_path: Path,
    embed_model: str,
    ollama_url: str,
) -> dict[str, int]:
    pending: dict[tuple[str, str], tuple[str, Path]] = {}

    for change, path in changes:
        changed = Path(path).expanduser().resolve()
        owner = _owner_repo(changed, repo_paths)
        if owner is None:
            continue
        repo_root, rel_path = owner
        key = (repo_root.as_posix(), rel_path)
        change_name = _change_name(change)
        current = pending.get(key)
        if current is None or change_name == "deleted":
            pending[key] = (change_name, changed)

    counts = {"indexed": 0, "deleted": 0, "failed": 0}
    for (repo_root_text, rel_path), (change_name, changed) in sorted(
        pending.items(), key=lambda item: item[0]
    ):
        repo_root = Path(repo_root_text)
        repo = repo_name(repo_root)
        if change_name == "deleted":
            removed = delete_indexed_path(collection, manifest, repo, rel_path)
            if removed:
                counts["deleted"] += 1
            else:
                counts["failed"] += 1
            continue

        try:
            changed_flag = index_file(
                collection=collection,
                path=changed,
                repo_root=repo_root,
                repo=repo,
                db_path=db_path,
                manifest=manifest,
                embed_model=embed_model,
                ollama_url=ollama_url,
                force=False,
            )
        except Exception as exc:
            print(f"index refresh failed for {repo}:{rel_path}: {exc}")
            counts["failed"] += 1
            continue
        if changed_flag:
            counts["indexed"] += 1

    return counts


def run_watch(
    repos: list[str],
    workspaces: list[str],
    db: str,
    collection_name: str,
    embed_model: str,
    ollama_url: str,
    debounce_seconds: float,
    initial_index: bool,
) -> None:
    from watchfiles import watch

    workspace_paths = [
        Path(workspace).expanduser().resolve() for workspace in workspaces
    ]
    repo_paths = resolve_repos(repos=repos, workspaces=workspaces)
    if not repo_paths:
        raise SystemExit("at least one --repo or --workspace is required")
    db_path = Path(db).expanduser().resolve()
    collection = open_collection(db_path, collection_name)

    if initial_index:
        print("initial index")
        run_index(
            repos=[str(repo) for repo in repo_paths],
            db=db,
            collection_name=collection_name,
            embed_model=embed_model,
            ollama_url=ollama_url,
        )
    manifest = load_manifest(db_path)

    print("watching:")
    for path in [*repo_paths, *workspace_paths]:
        print(f"  {path}")

    for changes in watch(
        *repo_paths, *workspace_paths, debounce=int(debounce_seconds * 1000)
    ):
        repo_paths = resolve_repos(repos=repos, workspaces=workspaces)
        start = time.monotonic()
        counts = _process_changes(
            changes=changes,
            repo_paths=repo_paths,
            collection=collection,
            manifest=manifest,
            db_path=db_path,
            embed_model=embed_model,
            ollama_url=ollama_url,
        )
        if not any(counts.values()):
            continue

        print(
            "detected "
            f"{counts['indexed']} update(s), {counts['deleted']} deletion(s), "
            f"{counts['failed']} failure(s)"
        )
        save_manifest(db_path, manifest)
        elapsed = time.monotonic() - start
        print(f"refresh complete in {elapsed:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch repos recursively and refresh the local Chroma code index."
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Repo path to watch. Repeat for multiple repos.",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        help="Workspace directory whose immediate Git child directories should be watched as repos. Repeat for multiple workspaces.",
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
        "--debounce-seconds",
        type=float,
        default=5.0,
        help="Seconds to debounce file changes. Default: 5",
    )
    parser.add_argument(
        "--no-initial-index",
        action="store_true",
        help="Start watching without indexing immediately.",
    )
    args = parser.parse_args()

    run_watch(
        repos=args.repo,
        workspaces=args.workspace,
        db=args.db,
        collection_name=args.collection,
        embed_model=args.embed_model,
        ollama_url=args.ollama_url,
        debounce_seconds=args.debounce_seconds,
        initial_index=not args.no_initial_index,
    )


if __name__ == "__main__":
    main()
