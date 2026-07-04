#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from local_code_context.storage.schema import ensure_schema, get_db_path, open_db
from local_code_context.storage.writer import index_file_xref
from local_code_context.storage.resolver import resolve_call_sites_for_repo, resolve_imports_for_repo
from local_code_context.syntax.indexer import build_index_records


DEFAULT_DB = "./codebase_index"

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


def load_manifest(db_path: Path) -> dict[str, str]:
    path = manifest_path(db_path)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_manifest(db_path: Path, manifest: dict[str, str]) -> None:
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


def index_file(
    path: Path,
    repo_root: Path,
    repo: str,
    db_path: Path,
    manifest: dict[str, str],
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
    if manifest.get(key) == digest:
        return False

    build_result = build_index_records(
        repo=repo,
        repo_root=repo_root,
        path=path,
        source=source,
        text=text,
    )

    index_file_xref(
        db_path=db_path,
        repo=repo,
        path=rel_path,
        extraction=build_result.extraction,
    )

    manifest[key] = digest
    return True


def run_index(
    repos: list[str],
    db: str = DEFAULT_DB,
    workspaces: list[str] | None = None,
) -> None:
    repo_paths = resolve_repos(repos=repos, workspaces=workspaces)
    if not repo_paths:
        raise SystemExit("at least one --repo or --workspace is required")

    db_path = Path(db).expanduser().resolve()
    manifest = load_manifest(db_path)

    total_changed = 0
    total_skipped = 0

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
                path=path,
                repo_root=repo_root,
                repo=repo,
                db_path=db_path,
                manifest=manifest,
            )
            if did_change:
                changed += 1
            else:
                skipped += 1

        for key in list(manifest.keys()):
            if key.startswith(f"{repo}:") and key not in seen_keys:
                del manifest[key]

        total_changed += changed
        total_skipped += skipped
        print(
            f"{repo}: indexed/updated {changed}, skipped {skipped}"
        )

        resolve_imports_for_repo(db_path=db_path, repo=repo)
        res = resolve_call_sites_for_repo(db_path=db_path, repo=repo)
        print(
            f"{repo}: call resolution: {res['resolved']} resolved, "
            f"{res['ambiguous']} ambiguous, {res['unresolved']} unresolved"
        )

    save_manifest(db_path, manifest)

    xref_db = get_db_path(db_path)
    xref_db.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(xref_db)
    try:
        ensure_schema(conn)
        for repo_root in repo_paths:
            repo = repo_name(repo_root)
            conn.execute(
                "INSERT OR REPLACE INTO repo_meta (repo, root_path, last_indexed) VALUES (?, ?, ?)",
                (repo, str(repo_root), ""),
            )
        conn.commit()
    finally:
        conn.close()

    print(
        f"done: indexed/updated {total_changed}, skipped {total_skipped}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index one or more repos into the xref SQLite database."
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
        "--db", default=DEFAULT_DB, help=f"DB directory. Default: {DEFAULT_DB}"
    )
    args = parser.parse_args()

    run_index(
        repos=args.repo,
        workspaces=args.workspace,
        db=args.db,
    )


if __name__ == "__main__":
    main()
