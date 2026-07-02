#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from pathlib import Path

from local_code_rag.index_repos import (
    DEFAULT_COLLECTION,
    DEFAULT_DB,
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_URL,
    resolve_repos,
    should_skip_path,
    run_index,
)


def relevant_change(path: str, repos: list[Path], workspaces: list[Path]) -> bool:
    changed = Path(path).resolve()
    for repo in repos:
        try:
            rel = changed.relative_to(repo)
        except ValueError:
            continue
        return not should_skip_path(rel)
    for workspace in workspaces:
        try:
            rel = changed.relative_to(workspace)
        except ValueError:
            continue
        return len(rel.parts) <= 2 and not should_skip_path(rel)
    return False


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

    workspace_paths = [Path(workspace).expanduser().resolve() for workspace in workspaces]
    repo_paths = resolve_repos(repos=repos, workspaces=workspaces)
    if not repo_paths:
        raise SystemExit("at least one --repo or --workspace is required")

    if initial_index:
        print("initial index")
        run_index(
            repos=[str(repo) for repo in repo_paths],
            db=db,
            collection_name=collection_name,
            embed_model=embed_model,
            ollama_url=ollama_url,
        )

    print("watching:")
    for path in [*repo_paths, *workspace_paths]:
        print(f"  {path}")

    for changes in watch(*repo_paths, *workspace_paths, debounce=int(debounce_seconds * 1000)):
        repo_paths = resolve_repos(repos=repos, workspaces=workspaces)
        relevant = [(change, path) for change, path in changes if relevant_change(path, repo_paths, workspace_paths)]
        if not relevant:
            continue

        print(f"detected {len(relevant)} relevant change(s); refreshing index")
        start = time.monotonic()
        try:
            run_index(
                repos=[str(repo) for repo in repo_paths],
                db=db,
                collection_name=collection_name,
                embed_model=embed_model,
                ollama_url=ollama_url,
            )
        except Exception as exc:
            print(f"index refresh failed: {exc}")
            continue
        elapsed = time.monotonic() - start
        print(f"refresh complete in {elapsed:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch repos recursively and refresh the local Chroma code index.")
    parser.add_argument("--repo", action="append", default=[], help="Repo path to watch. Repeat for multiple repos.")
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        help="Workspace directory whose immediate Git child directories should be watched as repos. Repeat for multiple workspaces.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Chroma DB directory. Default: {DEFAULT_DB}")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help=f"Chroma collection. Default: {DEFAULT_COLLECTION}")
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help=f"Ollama embedding model. Default: {DEFAULT_EMBED_MODEL}")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help=f"Ollama base URL. Default: {DEFAULT_OLLAMA_URL}")
    parser.add_argument("--debounce-seconds", type=float, default=5.0, help="Seconds to debounce file changes. Default: 5")
    parser.add_argument("--no-initial-index", action="store_true", help="Start watching without indexing immediately.")
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
