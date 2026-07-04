#!/usr/bin/env python3
from __future__ import annotations

import argparse
import time
from pathlib import Path

from local_code_context.indexing.indexer import (
    DEFAULT_DB,
    file_key,
    index_file,
    iter_files,
    load_manifest,
    parse_file_key,
    repo_name,
    resolve_repos,
    save_manifest,
    run_index,
)
from local_code_context.storage.resolver import resolve_repo_relationships
from local_code_context.storage.writer import delete_file_xref


def _owner_repo(path: Path, repos: list[Path]) -> tuple[Path, str] | None:
    for repo in repos:
        try:
            rel = path.relative_to(repo)
        except ValueError:
            continue
        return repo, rel.as_posix()
    return None


def _change_name(change: object) -> str:
    name = getattr(change, "name", None)
    if isinstance(name, str):
        return name
    return str(change)


def _stale_keys_for(
    manifest: dict[str, str],
    repo_root: Path,
) -> list[str]:
    repo = repo_name(repo_root)
    stale: list[str] = []
    for key in sorted(manifest):
        rel_path = parse_file_key(key, repo)
        if rel_path is not None and not (repo_root / rel_path).exists():
            stale.append(key)
    return stale


def _process_changes(
    *,
    changes: set[tuple[object, str]],
    repo_paths: list[Path],
    manifest: dict[str, str],
    db_path: Path,
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
            delete_file_xref(db_path, repo, rel_path)
            manifest.pop(f"{repo}:{rel_path}", None)
            counts["deleted"] += 1
            continue

        try:
            result = index_file(
                path=changed,
                repo_root=repo_root,
                repo=repo,
                db_path=db_path,
                manifest=manifest,
            )
        except Exception as exc:
            print(f"index refresh failed for {repo}:{rel_path}: {exc}")
            counts["failed"] += 1
            continue
        if result is True:
            counts["indexed"] += 1
        elif result is None:
            counts["failed"] += 1

    return counts


def _resolve_affected_repos(
    db_path: Path,
    affected_repos: set[str],
) -> None:
    for repo in sorted(affected_repos):
        stats = resolve_repo_relationships(db_path, repo)
        print(
            f"call resolution for {repo}: {stats['resolved']} resolved, "
            f"{stats['ambiguous']} ambiguous, {stats['unresolved']} unresolved"
        )


def run_watch(
    repos: list[str],
    workspaces: list[str],
    db: str,
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

    if initial_index:
        print("initial index")
        run_index(
            repos=[str(repo) for repo in repo_paths],
            db=db,
        )
    else:
        # Ensure call sites are resolved even without re-indexing
        for repo_root in repo_paths:
            resolve_call_sites_for_repo(db_path, repo_name(repo_root))

    manifest = load_manifest(db_path)

    affected_repos: set[str] = set()
    for repo_root in repo_paths:
        repo = repo_name(repo_root)
        for key in _stale_keys_for(manifest, repo_root):
            rel_path = parse_file_key(key, repo)
            if rel_path is not None:
                delete_file_xref(db_path, repo, rel_path)
            del manifest[key]
            affected_repos.add(repo)
    for repo in sorted(affected_repos):
        resolve_repo_relationships(db_path, repo)
    if manifest:
        save_manifest(db_path, manifest)

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
            manifest=manifest,
            db_path=db_path,
        )
        if not any(counts.values()):
            continue

        print(
            "detected "
            f"{counts['indexed']} update(s), {counts['deleted']} deletion(s), "
            f"{counts['failed']} failure(s)"
        )

        for repo_root in repo_paths:
            stale = _stale_keys_for(manifest, repo_root)
            if stale:
                repo = repo_name(repo_root)
                affected_repos.add(repo)
                for key in stale:
                    rel_path = parse_file_key(key, repo)
                    if rel_path is not None:
                        delete_file_xref(db_path, repo, rel_path)
                    del manifest[key]
                counts["deleted"] += len(stale)

        for _, path in changes:
            changed = Path(path).expanduser().resolve()
            owner = _owner_repo(changed, repo_paths)
            if owner is not None:
                affected_repos.add(repo_name(owner[0]))

        # Save manifest BEFORE resolution so a crash leaves DB ↔ manifest consistent
        save_manifest(db_path, manifest)

        _resolve_affected_repos(db_path, affected_repos)
        elapsed = time.monotonic() - start
        print(f"refresh complete in {elapsed:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watch repos recursively and refresh the local code index."
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
        "--db", default=DEFAULT_DB, help=f"DB directory. Default: {DEFAULT_DB}"
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
        debounce_seconds=args.debounce_seconds,
        initial_index=not args.no_initial_index,
    )


if __name__ == "__main__":
    main()
