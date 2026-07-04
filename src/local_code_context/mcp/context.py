from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from local_code_context.indexing.indexer import (
    SKIP_SUFFIXES as IGNORED_SUFFIXES,
    iter_files,
    should_skip_path,
)
from local_code_context.storage.schema import get_db_path, open_db


IMPORTANT_FILES = [
    "README.md",
    "README",
    "flake.nix",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Makefile",
    "justfile",
    "docker-compose.yml",
    "compose.yml",
]

DEFAULT_REPOSITORY_MAX_CHARS = 30_000
DEFAULT_WORKSPACE_MAX_CHARS = 8_000

README_MAX_CHARS = 6_000
EXCERPT_MAX_CHARS = 4_000
TREE_MAX_ENTRIES = 72
COMPACT_TREE_MAX_ENTRIES = 28
COMPACT_EXCERPT_MAX_CHARS = 1_500

SOURCE_DIR_PREFIXES = {
    "src",
    "lib",
    "app",
    "cmd",
    "internal",
    "pkg",
    "tests",
    "test",
}

CONFIG_NAME_PATTERNS = (
    "config",
    "settings",
    "env",
    ".env",
    "profiles",
    "secrets",
)

PERSISTENCE_PATTERNS = (
    "chromadb",
    "ollama",
    "sqlite",
    "postgres",
    "redis",
    "requests",
    "http://",
    "https://",
    "fetch(",
    "PersistentClient",
)


@dataclass(frozen=True)
class IndexedRepository:
    repo: str
    repo_root: Path | None


def _discover_repo_records(db_path: Path) -> list[IndexedRepository]:
    xref_db = get_db_path(db_path)
    if not xref_db.exists():
        return []
    conn = open_db(xref_db)
    try:
        rows = conn.execute(
            "SELECT repo, root_path FROM repo_meta WHERE repo != '__schema__' AND root_path != ''"
        ).fetchall()
        records: list[IndexedRepository] = []
        seen: set[str] = set()
        for row in rows:
            repo = row["repo"]
            root = Path(row["root_path"]).expanduser().resolve()
            if repo in seen:
                continue
            seen.add(repo)
            records.append(IndexedRepository(repo=repo, repo_root=root if root.exists() else None))
        return records
    finally:
        conn.close()


def list_indexed_repositories(db_path: Path) -> list[str]:
    return [record.repo for record in _discover_repo_records(db_path)]


def _resolve_repo_name(db_path: Path, repo: str) -> IndexedRepository | None:
    records = _discover_repo_records(db_path)

    for record in records:
        if record.repo == repo:
            return record

    basename = Path(repo).name
    matches = [r for r in records if Path(r.repo).name == basename]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"repository name {repo!r} is ambiguous; matched multiple repositories: "
            + ", ".join(m.repo for m in matches)
        )

    return None


def _require_repo_record(db_path: Path, repo: str) -> IndexedRepository:
    result = _resolve_repo_name(db_path, repo)
    if result is not None:
        return result

    indexed = ", ".join(r.repo for r in _discover_repo_records(db_path)) or "(none)"
    raise ValueError(
        f"repository {repo!r} is not indexed. Indexed repositories: {indexed}"
    )


def _require_repo_root(db_path: Path, repo: str) -> Path:
    record = _require_repo_record(db_path, repo)
    if record.repo_root is None:
        raise ValueError(
            f"repository root is missing from the index metadata for {repo!r}. "
            "Rebuild the index with a newer version of local-code-context."
        )
    root = record.repo_root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(
            f"indexed repository root does not exist for {repo!r}: {root}. "
            "Rebuild the index or update the stored repo_root metadata."
        )
    return root


def _resolve_repo_path(repo_root: Path, relative_path: str | Path) -> Path:
    candidate = (repo_root / Path(relative_path)).expanduser().resolve()
    if candidate != repo_root and repo_root not in candidate.parents:
        raise ValueError(
            f"refusing to read path outside repository root: {relative_path!s}"
        )
    return candidate


def _is_text_path(path: Path) -> bool:
    if path.name in {".DS_Store"}:
        return False
    if path.suffix.lower() in IGNORED_SUFFIXES:
        return False
    return True


def _read_text(path: Path, max_chars: int | None = None) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None

    if b"\x00" in data:
        return None

    text = data.decode("utf-8", errors="replace")
    if max_chars is not None and max_chars >= 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _relative_path(repo_root: Path, path: Path) -> str:
    return path.relative_to(repo_root).as_posix()


def _path_priority(relative_path: Path) -> tuple[int, int, str]:
    parts = relative_path.parts
    top = parts[0] if parts else ""
    name = relative_path.name

    if name in IMPORTANT_FILES:
        return (0, len(parts), relative_path.as_posix())
    if (
        top in {"tests", "test", "__tests__"}
        or name.startswith("test_")
        or name.endswith(
            (".test.py", ".spec.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
        )
    ):
        return (1, len(parts), relative_path.as_posix())
    if top in SOURCE_DIR_PREFIXES or relative_path.suffix in {
        ".py",
        ".rs",
        ".go",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".nix",
    }:
        return (2, len(parts), relative_path.as_posix())
    if any(part in {"config", "configs", "settings", "scripts"} for part in parts):
        return (3, len(parts), relative_path.as_posix())
    return (4, len(parts), relative_path.as_posix())


def _compact_paths(
    files: Iterable[Path], repo_root: Path, max_entries: int
) -> list[Path]:
    paths: list[Path] = []
    for path in files:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if should_skip_path(rel):
            continue
        paths.append(rel)

    ranked = sorted(set(paths), key=_path_priority)
    selected = ranked[:max_entries]
    return [repo_root / rel for rel in selected]


def _priority_tree_paths(
    files: Iterable[Path], repo_root: Path, max_entries: int
) -> list[Path]:
    selected = _compact_paths(files, repo_root, max_entries)
    result: set[Path] = set()
    for path in selected:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        result.add(rel)
        for parent in rel.parents:
            if not parent.parts:
                continue
            result.add(parent)
    return [repo_root / rel for rel in sorted(result, key=lambda p: p.as_posix())]


def _ascii_tree(paths: Iterable[Path], repo_root: Path, max_depth: int = 3) -> str:
    tree: dict[str, dict[str, Any]] = {}

    for path in paths:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if not rel.parts:
            continue
        if len(rel.parts) > max_depth + 1:
            rel = Path(*rel.parts[: max_depth + 1])
        node = tree
        for part in rel.parts:
            node = node.setdefault(part, {})

    lines = ["."]

    def walk(node: dict[str, Any], prefix: str = "") -> None:
        items = sorted(node.items())
        for index, (name, child) in enumerate(items):
            is_last = index == len(items) - 1
            branch = "└── " if is_last else "├── "
            lines.append(f"{prefix}{branch}{name}{'/' if child else ''}")
            if child:
                walk(child, prefix + ("    " if is_last else "│   "))

    walk(tree)
    return "\n".join(lines)


def _render_numbered_excerpt(
    text: str, max_lines: int = 80, max_chars: int | None = None
) -> str:
    lines = text.splitlines()
    rendered: list[str] = []
    char_count = 0
    for index, line in enumerate(lines[:max_lines], start=1):
        formatted = f"{index:>4}: {line}"
        rendered.append(formatted)
        char_count += len(formatted) + 1
        if max_chars is not None and char_count >= max_chars:
            break
    if len(lines) > max_lines:
        rendered.append("[... truncated ...]")
    return "\n".join(rendered)


def _find_files(repo_root: Path, files: Iterable[Path], names: set[str]) -> list[Path]:
    matches: list[Path] = []
    for path in files:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if rel.name in names:
            matches.append(path)
    return sorted(matches, key=lambda p: _relative_path(repo_root, p))


def _find_first(repo_root: Path, files: Iterable[Path], names: set[str]) -> Path | None:
    matches = _find_files(repo_root, files, names)
    return matches[0] if matches else None


def _tracked_sources(repo_root: Path, files: Iterable[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in files:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in {"tests", "test", "__tests__"}:
            continue
        if rel.name in IMPORTANT_FILES:
            continue
        if rel.suffix.lower() not in {
            ".py",
            ".rs",
            ".go",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".sh",
            ".nix",
        }:
            continue
        selected.append(path)
    return sorted(
        selected,
        key=lambda p: (
            _path_priority(p.relative_to(repo_root)),
            _relative_path(repo_root, p),
        ),
    )


def _manifest_files(repo_root: Path, files: Iterable[Path]) -> list[Path]:
    wanted = {
        "README.md",
        "README",
        "pyproject.toml",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "flake.nix",
        "Makefile",
        "justfile",
        "docker-compose.yml",
        "compose.yml",
    }
    return _find_files(repo_root, files, wanted)


def _read_manifest_summary(path: Path, repo_root: Path) -> str:
    text = _read_text(path, max_chars=EXCERPT_MAX_CHARS)
    if text is None:
        return "(unreadable)"

    rel = _relative_path(repo_root, path)
    if path.name == "pyproject.toml":
        try:
            import tomllib

            data = tomllib.loads(text)
        except Exception:
            return _render_numbered_excerpt(text, max_lines=60)

        lines = [f"{rel}"]
        project = data.get("project", {})
        scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
        if isinstance(project, dict):
            name = project.get("name")
            if isinstance(name, str):
                lines.append(f"- project.name: {name}")
            dependencies = project.get("dependencies")
            if isinstance(dependencies, list) and dependencies:
                lines.append(f"- project.dependencies: {len(dependencies)} entries")
        if isinstance(scripts, dict) and scripts:
            lines.append("- project.scripts:")
            for script_name, target in sorted(scripts.items()):
                lines.append(f"  - {script_name} = {target}")
        tool = data.get("tool", {})
        if isinstance(tool, dict):
            poetry = tool.get("poetry", {})
            if isinstance(poetry, dict):
                poetry_scripts = poetry.get("scripts", {})
                if isinstance(poetry_scripts, dict) and poetry_scripts:
                    lines.append("- tool.poetry.scripts:")
                    for script_name, target in sorted(poetry_scripts.items()):
                        lines.append(f"  - {script_name} = {target}")
        return "\n".join(lines)

    if path.name == "package.json":
        try:
            data = json.loads(text)
        except Exception:
            return _render_numbered_excerpt(text, max_lines=60)
        lines = [f"{rel}"]
        if isinstance(data, dict):
            if isinstance(data.get("name"), str):
                lines.append(f"- name: {data['name']}")
            scripts = data.get("scripts", {})
            if isinstance(scripts, dict) and scripts:
                lines.append("- scripts:")
                for script_name, target in sorted(scripts.items()):
                    lines.append(f"  - {script_name} = {target}")
            deps = data.get("dependencies", {})
            if isinstance(deps, dict) and deps:
                lines.append(f"- dependencies: {len(deps)}")
            dev_deps = data.get("devDependencies", {})
            if isinstance(dev_deps, dict) and dev_deps:
                lines.append(f"- devDependencies: {len(dev_deps)}")
        return "\n".join(lines)

    if path.name == "Cargo.toml":
        try:
            import tomllib

            data = tomllib.loads(text)
        except Exception:
            return _render_numbered_excerpt(text, max_lines=60)
        lines = [f"{rel}"]
        package = data.get("package", {})
        if isinstance(package, dict):
            for key in ("name", "version", "edition"):
                value = package.get(key)
                if isinstance(value, str):
                    lines.append(f"- {key}: {value}")
        deps = data.get("dependencies", {})
        if isinstance(deps, dict) and deps:
            lines.append(f"- dependencies: {len(deps)}")
        bins = data.get("bin")
        if isinstance(bins, list) and bins:
            lines.append(f"- [[bin]] entries: {len(bins)}")
        return "\n".join(lines)

    if path.name == "go.mod":
        return _render_numbered_excerpt(text, max_lines=30)

    if path.name == "flake.nix":
        return _render_numbered_excerpt(text, max_lines=100)

    return _render_numbered_excerpt(text, max_lines=60)


def _read_readme(repo_root: Path, files: Iterable[Path], compact: bool) -> str:
    readme = _find_first(repo_root, files, {"README.md", "README"})
    if readme is None:
        return "(no README found)"
    text = _read_text(
        readme, max_chars=README_MAX_CHARS if not compact else COMPACT_EXCERPT_MAX_CHARS
    )
    if text is None:
        return "(unreadable README)"
    return _render_numbered_excerpt(text, max_lines=80 if not compact else 35)


def _discover_entry_points(repo_root: Path, files: Iterable[Path]) -> list[str]:
    entries: list[str] = []
    manifest = _find_first(repo_root, files, {"pyproject.toml"})
    if manifest is not None:
        text = _read_text(manifest, max_chars=EXCERPT_MAX_CHARS)
        if text:
            try:
                import tomllib

                data = tomllib.loads(text)
                project = data.get("project", {})
                if isinstance(project, dict):
                    scripts = project.get("scripts", {})
                    if isinstance(scripts, dict):
                        for name, target in sorted(scripts.items()):
                            entries.append(
                                f"pyproject.toml [project.scripts] {name} = {target}"
                            )
                tool = data.get("tool", {})
                if isinstance(tool, dict):
                    poetry = tool.get("poetry", {})
                    if isinstance(poetry, dict):
                        scripts = poetry.get("scripts", {})
                        if isinstance(scripts, dict):
                            for name, target in sorted(scripts.items()):
                                entries.append(
                                    f"pyproject.toml [tool.poetry.scripts] {name} = {target}"
                                )
            except Exception:
                pass

    package_json = _find_first(repo_root, files, {"package.json"})
    if package_json is not None:
        text = _read_text(package_json, max_chars=EXCERPT_MAX_CHARS)
        if text:
            try:
                data = json.loads(text)
                scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
                if isinstance(scripts, dict):
                    for name, target in sorted(scripts.items()):
                        entries.append(f"package.json scripts {name} = {target}")
                bins = data.get("bin", {}) if isinstance(data, dict) else {}
                if isinstance(bins, str):
                    entries.append(f"package.json bin = {bins}")
                elif isinstance(bins, dict):
                    for name, target in sorted(bins.items()):
                        entries.append(f"package.json bin {name} = {target}")
            except Exception:
                pass

    for path in files:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if rel.name in {"main.py", "__main__.py"}:
            entries.append(rel.as_posix())
        if rel.as_posix() == "src/main.rs":
            entries.append(rel.as_posix())
        if rel.parts and rel.parts[0] == "cmd":
            entries.append(rel.as_posix())
        if path.is_file():
            try:
                mode = path.stat().st_mode
            except OSError:
                mode = 0
            if mode & 0o111 and rel.suffix in {"", ".sh"}:
                entries.append(f"executable {rel.as_posix()}")

    unique: list[str] = []
    for entry in entries:
        if entry not in unique:
            unique.append(entry)
    return unique


def _discover_major_modules(repo_root: Path, files: Iterable[Path]) -> list[str]:
    roots: dict[str, int] = {}
    for path in files:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in {"tests", "test", "__tests__"}:
            continue
        if rel.suffix.lower() not in {
            ".py",
            ".rs",
            ".go",
            ".ts",
            ".tsx",
            ".js",
            ".jsx",
            ".nix",
            ".sh",
        }:
            continue
        if rel.parts and rel.parts[0] in SOURCE_DIR_PREFIXES:
            root = rel.parts[0]
            if len(rel.parts) > 1 and rel.parts[0] == "src":
                root = "/".join(rel.parts[:2])
            roots[root] = roots.get(root, 0) + 1
        else:
            roots[rel.parent.as_posix()] = roots.get(rel.parent.as_posix(), 0) + 1

    ranked = sorted(roots.items(), key=lambda item: (-item[1], item[0]))
    return [root for root, _ in ranked[:12] if root not in {"", "."}]


def _discover_config_files(repo_root: Path, files: Iterable[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in files:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        rel_text = rel.as_posix().lower()
        if rel.name.startswith(".env") or any(
            pattern in rel_text for pattern in CONFIG_NAME_PATTERNS
        ):
            selected.append(path)
            continue
        if (
            rel.suffix.lower()
            in {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf"}
            and rel.name not in IMPORTANT_FILES
        ):
            selected.append(path)
    return sorted(
        {path for path in selected}, key=lambda p: _relative_path(repo_root, p)
    )


def _discover_persistence_files(repo_root: Path, files: Iterable[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in files:
        text = _read_text(path, max_chars=EXCERPT_MAX_CHARS)
        if not text:
            continue
        lowered = text.lower()
        if any(pattern.lower() in lowered for pattern in PERSISTENCE_PATTERNS):
            selected.append(path)
    return sorted(
        {path for path in selected}, key=lambda p: _relative_path(repo_root, p)
    )


def _discover_test_files(repo_root: Path, files: Iterable[Path]) -> list[Path]:
    selected: list[Path] = []
    for path in files:
        try:
            rel = path.relative_to(repo_root)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in {"tests", "test", "__tests__"}:
            selected.append(path)
            continue
        if rel.name.startswith("test_") or rel.name.endswith(
            (".test.py", ".spec.py", ".test.ts", ".spec.ts", ".test.js", ".spec.js")
        ):
            selected.append(path)
    return sorted(
        {path for path in selected}, key=lambda p: _relative_path(repo_root, p)
    )


def _selected_excerpts(
    repo_root: Path, paths: list[Path], max_files: int, compact: bool
) -> str:
    blocks: list[str] = []
    for path in paths[:max_files]:
        text = _read_text(
            path, max_chars=COMPACT_EXCERPT_MAX_CHARS if compact else EXCERPT_MAX_CHARS
        )
        if not text:
            continue
        blocks.append(
            f"{_relative_path(repo_root, path)}\n"
            f"```text\n{_render_numbered_excerpt(text, max_lines=35 if compact else 80)}\n```"
        )
    return "\n\n".join(blocks) if blocks else "(no source excerpts selected)"


def _section(title: str, body: str) -> str:
    return f"=== {title} ===\n{body.rstrip()}\n"


def _render_sections(sections: list[tuple[str, str]], max_chars: int) -> str:
    chunks: list[str] = []
    used = 0

    for title, body in sections:
        block = _section(title, body)
        if used + len(block) <= max_chars:
            chunks.append(block)
            used += len(block)
            continue

        remaining = max_chars - used
        if remaining <= 0:
            break

        header = f"=== {title} ===\n"
        if remaining <= len(header):
            chunks.append(header[:remaining])
            used = max_chars
            break

        body_room = remaining - len(header)
        marker = "\n[Context truncated]\n"
        slice_room = max(0, body_room - len(marker))
        chunks.append(header + body[:slice_room].rstrip() + marker)
        used = max_chars
        break

    rendered = "\n".join(chunks).rstrip()
    if len(rendered) > max_chars:
        rendered = rendered[:max_chars].rstrip()
    if (
        rendered
        and not rendered.endswith("[Context truncated]")
        and len(chunks) < len(sections)
    ):
        suffix = "\n[Context truncated]"
        if len(rendered) + len(suffix) <= max_chars:
            rendered += suffix
    return rendered


def _build_repository_sections(
    db_path: Path,
    repo: str,
    *,
    max_chars: int,
    compact: bool,
) -> list[tuple[str, str]]:
    repo_root = _require_repo_root(db_path, repo)
    files = iter_files(repo_root)

    important_files = [
        path
        for path in _manifest_files(repo_root, files)
        if path.name not in {"README", "README.md"}
    ]
    readme = _read_readme(repo_root, files, compact=compact)
    tree_paths = _priority_tree_paths(
        files,
        repo_root,
        COMPACT_TREE_MAX_ENTRIES if compact else TREE_MAX_ENTRIES,
    )
    entry_points = _discover_entry_points(repo_root, files)
    major_modules = _discover_major_modules(repo_root, files)
    config_files = _discover_config_files(repo_root, files)
    persistence_files = _discover_persistence_files(repo_root, files)
    test_files = _discover_test_files(repo_root, files)

    tree = _ascii_tree(tree_paths, repo_root)
    manifest_body = (
        "\n\n".join(_read_manifest_summary(path, repo_root) for path in important_files)
        or "(no build or dependency manifests found)"
    )

    entry_body = (
        "\n".join(f"- {entry}" for entry in entry_points)
        or "(no obvious entry points found)"
    )
    major_body = (
        "\n".join(f"- {root}" for root in major_modules)
        or "(no obvious major modules found)"
    )

    if config_files:
        config_body = "\n\n".join(
            f"{_relative_path(repo_root, path)}\n```text\n{_render_numbered_excerpt(_read_text(path, max_chars=COMPACT_EXCERPT_MAX_CHARS if compact else EXCERPT_MAX_CHARS) or '', max_lines=30 if compact else 60)}\n```"
            for path in config_files[: 4 if compact else 8]
        )
    else:
        config_body = "(no configuration files found)"

    if persistence_files:
        persistence_body = "\n\n".join(
            f"{_relative_path(repo_root, path)}\n```text\n{_render_numbered_excerpt(_read_text(path, max_chars=COMPACT_EXCERPT_MAX_CHARS if compact else EXCERPT_MAX_CHARS) or '', max_lines=25 if compact else 50)}\n```"
            for path in persistence_files[: 4 if compact else 8]
        )
    else:
        persistence_body = "(no persistence or external-service integrations found)"

    tests_body = (
        "\n".join(f"- {_relative_path(repo_root, path)}" for path in test_files)
        or "(no tests found)"
    )
    excerpts = _selected_excerpts(
        repo_root,
        [
            *important_files,
            *[
                path
                for path in _tracked_sources(repo_root, files)
                if path not in important_files
            ],
        ],
        3 if compact else 6,
        compact,
    )

    sections = [
        ("Repository", f"{repo}\n{repo_root}"),
        ("File tree", tree),
        ("README", readme),
        ("Build and dependencies", manifest_body),
        ("Entry points", entry_body),
        ("Major modules", major_body),
        ("Configuration", config_body),
        ("Persistence and external services", persistence_body),
        ("Tests", tests_body),
        ("Selected source excerpts", excerpts),
    ]
    return sections


def get_repository_context(db_path: Path, repo: str, max_chars: int | None = None) -> str:
    budget = (
        DEFAULT_REPOSITORY_MAX_CHARS if max_chars is None else max(1, int(max_chars))
    )
    sections = _build_repository_sections(db_path, repo, max_chars=budget, compact=False)
    return _render_sections(sections, budget)


def get_workspace_context(
    db_path: Path,
    repos: list[str] | None = None,
    max_chars_per_repo: int | None = None,
) -> str:
    budget = (
        DEFAULT_WORKSPACE_MAX_CHARS
        if max_chars_per_repo is None
        else max(1, int(max_chars_per_repo))
    )
    indexed = list_indexed_repositories(db_path)
    if repos is None:
        wanted = indexed
    else:
        wanted = [_require_repo_record(db_path, repo).repo for repo in repos]

    packets: list[str] = []
    for repo in wanted:
        sections = _build_repository_sections(
            db_path, repo, max_chars=budget, compact=True
        )
        packets.append(_render_sections(sections, budget))
    return "\n\n".join(packets)
