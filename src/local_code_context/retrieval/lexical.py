from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from local_code_context.retrieval.models import HybridCandidate
from local_code_context.retrieval.query import get_collection

_STOP_WORDS: set[str] = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "need",
    "dare", "ought", "used", "this", "that", "these", "those", "i", "me",
    "my", "we", "our", "you", "your", "he", "she", "it", "they", "them",
    "its", "his", "her", "their", "not", "no", "nor", "so", "if", "then",
    "than", "too", "very", "just", "about", "also", "how", "what", "when",
    "where", "why", "which", "who", "whom", "does", "doing", "done",
    "get", "gets", "getting", "got", "make", "makes", "making", "made",
    "each", "every", "all", "both", "few", "more", "most", "some", "any",
    "into", "onto", "upon", "over", "under", "between", "through",
    "during", "before", "after", "above", "below", "up", "down", "out",
    "off", "here", "there",
}

_STOP_WORDS_MIN = 3


def _extract_terms(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z_]\w*", query)
    return [
        w.lower()
        for w in words
        if len(w) >= _STOP_WORDS_MIN and w.lower() not in _STOP_WORDS
    ]


def _repo_root_for(
    config: Any, repo: str, indexed_repos: list[dict[str, Any]] | None = None
) -> Path | None:
    if indexed_repos is None:
        collection = get_collection(db_path=config.db, collection_name=config.collection)
        payload = collection.get(include=["metadatas"])
        from collections import defaultdict
        roots: dict[str, set[Path]] = defaultdict(set)
        for m in (payload.get("metadatas") or []):
            if isinstance(m, dict) and m.get("repo") == repo:
                r = m.get("repo_root")
                if isinstance(r, str) and r.strip():
                    roots[repo].add(Path(r).expanduser().resolve())
        if repo in roots:
            return next(iter(roots[repo]))
        return None

    for entry in indexed_repos:
        if entry.get("repo") == repo:
            r = entry.get("repo_root")
            if isinstance(r, str) and r.strip():
                return Path(r).expanduser().resolve()
    return None


def _path_for_record(
    config: Any,
    repo: str,
    rel_path: str,
    repo_root_cache: dict[str, Path | None],
) -> Path | None:
    root = repo_root_cache.get(repo)
    if root is None:
        root = _repo_root_for(config, repo)
        repo_root_cache[repo] = root
    if root is None:
        return None
    return root / rel_path


def _run_rg(
    query: str,
    repo_paths: list[tuple[str, Path]],
    repo_filter: str | None,
    path_filter: str | None,
    max_results: int,
) -> list[tuple[str, str]]:
    terms = _extract_terms(query)
    if not terms:
        return []

    filtered_repos = repo_paths
    if repo_filter:
        filtered_repos = [(r, p) for r, p in repo_paths if r == repo_filter]

    results: list[tuple[str, str]] = []
    seen_paths: set[str] = set()

    for repo_name, repo_root in filtered_repos:
        args = ["rg", "-n", "--no-heading", "-i"]
        if path_filter:
            args.extend(["--glob", f"**/{path_filter}*"])
            args.extend(["--glob", f"**/{path_filter}/**"])
        args.extend(["-e", "|".join(re.escape(t) for t in terms[:8])])
        args.append(str(repo_root))

        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=15,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

        for line in proc.stdout.strip().split("\n")[:max_results]:
            if not line.strip():
                continue
            parts = line.split(":", 1)
            if len(parts) < 2:
                continue
            raw_path = parts[0]
            try:
                rel = Path(raw_path).relative_to(repo_root).as_posix()
            except ValueError:
                continue
            key = f"{repo_name}:{rel}"
            if key not in seen_paths:
                seen_paths.add(key)
                results.append((repo_name, rel))

    return results


def _fetch_records_by_path(
    config: Any,
    repo: str,
    rel_path: str,
) -> list[dict[str, Any]]:
    collection = get_collection(db_path=config.db, collection_name=config.collection)
    where: dict[str, Any] = {
        "$and": [
            {"repo": {"$eq": repo}},
            {"path": {"$eq": rel_path}},
        ]
    }
    payload = collection.get(
        where=where,
        limit=100,
        include=["documents", "metadatas"],
    )
    docs = payload.get("documents") or []
    metas = payload.get("metadatas") or []
    records: list[dict[str, Any]] = []
    for doc, meta in zip(docs, metas):
        records.append({"document": doc, "metadata": meta})
    return records


def lexical_search(
    config: Any,
    query: str,
    repo: str | None = None,
    path: str | None = None,
    max_results: int = 20,
) -> list[HybridCandidate]:
    collection = get_collection(db_path=config.db, collection_name=config.collection)
    all_metas = collection.get(include=["metadatas"]).get("metadatas") or []

    repo_roots: dict[str, set[Path]] = {}
    for m in all_metas:
        if isinstance(m, dict):
            r = m.get("repo")
            root_raw = m.get("repo_root")
            if isinstance(r, str) and isinstance(root_raw, str) and root_raw.strip():
                repo_roots.setdefault(r, set()).add(Path(root_raw).expanduser().resolve())

    repo_paths: list[tuple[str, Path]] = []
    for r, roots in repo_roots.items():
        if roots:
            repo_paths.append((r, next(iter(roots))))

    rg_hits = _run_rg(query, repo_paths, repo, path, max_results * 2)

    candidates: list[HybridCandidate] = []
    seen_composite: set[tuple[Any, ...]] = set()

    for hit_repo, hit_path in rg_hits:
        records = _fetch_records_by_path(config, hit_repo, hit_path)
        for rec in records:
            meta: dict[str, Any] = rec["metadata"]
            doc: str = rec["document"]
            comp_key = (
                meta.get("repo", ""),
                meta.get("path", ""),
                meta.get("chunk_type", ""),
                meta.get("symbol", ""),
                meta.get("symbol_kind", ""),
                meta.get("parent_symbol", ""),
                meta.get("start_line"),
                meta.get("end_line"),
                meta.get("part_index"),
            )
            if comp_key in seen_composite:
                continue
            seen_composite.add(comp_key)
            candidates.append(HybridCandidate(
                record_id=meta.get("id"),
                repo=meta.get("repo", ""),
                path=meta.get("path", ""),
                language=meta.get("language"),
                chunk_type=meta.get("chunk_type"),
                symbol=meta.get("symbol"),
                symbol_kind=meta.get("symbol_kind"),
                parent_symbol=meta.get("parent_symbol"),
                start_line=meta.get("start_line"),
                end_line=meta.get("end_line"),
                part_index=meta.get("part_index"),
                document=doc,
                lexical_score=1.0,
                match_sources=["lexical"],
            ))

    return candidates[:max_results]
