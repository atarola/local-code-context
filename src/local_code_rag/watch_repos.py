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
    should_skip_path,
    run_index,
)


def relevant_change(path: str, repos: list[Path]) -> bool:
    changed = Path(path).resolve()
    for repo in repos:
        try:
            rel = changed.relative_to(repo)
        except ValueError:
            continue
        return not should_skip_path(rel)
    return False


def run_watch(
    repos: list[str],
    db: str,
    collection_name: str,
    embed_model: str,
    ollama_url: str,
    debounce_seconds: float,
    initial_index: bool,
) -> None:
    from watchfiles import watch

    repo_paths = [Path(repo).expanduser().resolve() for repo in repos]
    for repo in repo_paths:
        if not repo.exists() or not repo.is_dir():
            raise SystemExit(f"repo does not exist or is not a directory: {repo}")

    if initial_index:
        print("initial index")
        run_index(repos=[str(repo) for repo in repo_paths], db=db, collection_name=collection_name, embed_model=embed_model, ollama_url=ollama_url)

    print("watching:")
    for repo in repo_paths:
        print(f"  {repo}")

    for changes in watch(*repo_paths, debounce=int(debounce_seconds * 1000)):
        relevant = [(change, path) for change, path in changes if relevant_change(path, repo_paths)]
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
    parser.add_argument("--repo", action="append", required=True, help="Repo path to watch. Repeat for multiple repos.")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"Chroma DB directory. Default: {DEFAULT_DB}")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help=f"Chroma collection. Default: {DEFAULT_COLLECTION}")
    parser.add_argument("--embed-model", default=DEFAULT_EMBED_MODEL, help=f"Ollama embedding model. Default: {DEFAULT_EMBED_MODEL}")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help=f"Ollama base URL. Default: {DEFAULT_OLLAMA_URL}")
    parser.add_argument("--debounce-seconds", type=float, default=5.0, help="Seconds to debounce file changes. Default: 5")
    parser.add_argument("--no-initial-index", action="store_true", help="Start watching without indexing immediately.")
    args = parser.parse_args()

    run_watch(
        repos=args.repo,
        db=args.db,
        collection_name=args.collection,
        embed_model=args.embed_model,
        ollama_url=args.ollama_url,
        debounce_seconds=args.debounce_seconds,
        initial_index=not args.no_initial_index,
    )


if __name__ == "__main__":
    main()
