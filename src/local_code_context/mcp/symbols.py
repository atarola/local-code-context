from __future__ import annotations

from collections import defaultdict
from typing import Any

from local_code_context.retrieval.query import get_collection

MAX_RESULTS = 200
INTERNAL_FETCH_LIMIT = 1000


def _single_key(meta: dict[str, Any]) -> tuple[Any, ...]:
    return (
        meta["repo"],
        meta["path"],
        meta["symbol"],
        meta["symbol_kind"],
        meta["parent_symbol"],
        meta["start_line"],
        meta["end_line"],
        meta["part_count"],
    )


def _part_group_key(meta: dict[str, Any]) -> tuple[Any, ...]:
    return (
        meta["repo"],
        meta["path"],
        meta["symbol"],
        meta["symbol_kind"],
        meta["parent_symbol"],
        meta["part_count"],
    )


def _validate_parts(
    parts: list[tuple[dict[str, Any], str]],
) -> list[str]:
    warnings: list[str] = []
    part_indices = [p[0]["part_index"] for p in parts]
    part_count = parts[0][0]["part_count"]

    for p in parts:
        if p[0]["part_count"] != part_count:
            warnings.append(
                f"inconsistent part_count within group: expected {part_count}, "
                f"got {p[0]['part_count']}"
            )

    if len(set(part_indices)) != len(part_indices):
        warnings.append("duplicate part_index values found")

    expected = set(range(1, part_count + 1))
    missing = expected - set(part_indices)
    if missing:
        sorted_missing = sorted(missing)
        warnings.append(
            f"incomplete part set: missing part(s) {sorted_missing} "
            f"(have {sorted(part_indices)} of {part_count})"
        )

    return warnings


def _format_single(
    meta: dict[str, Any],
    doc: str,
) -> str:
    lines = [
        f"Repository: {meta['repo']}",
        f"Path: {meta['path']}",
        f"Language: {meta.get('language', '')}",
        f"Kind: {meta['symbol_kind']}",
        f"Parent: {meta['parent_symbol']}",
        f"Lines: {meta['start_line']}-{meta['end_line']}",
        "",
        "Source:",
        "",
    ]
    for i, src_line in enumerate(doc.splitlines(), start=meta["start_line"]):
        lines.append(f"   {i:>4}: {src_line}")
    return "\n".join(lines)


def _format_multipart(
    parts: list[tuple[dict[str, Any], str]],
    warnings: list[str],
) -> str:
    first = parts[0][0]
    last = parts[-1][0]
    lines = [
        f"Repository: {first['repo']}",
        f"Path: {first['path']}",
        f"Language: {first.get('language', '')}",
        f"Kind: {first['symbol_kind']}",
        f"Parent: {first['parent_symbol']}",
        f"Lines: {first['start_line']}-{last['end_line']}",
        f"Parts: {first['part_count']}",
        "",
    ]
    if warnings:
        for w in warnings:
            lines.append(f"Warning: {w}")
        lines.append("")

    for idx, (meta, doc) in enumerate(parts, start=1):
        p_start = meta["start_line"]
        p_end = meta["end_line"]
        lines.append(f"Part {idx}/{len(parts)} (lines {p_start}-{p_end}):")
        lines.append("")
        for i, src_line in enumerate(doc.splitlines(), start=p_start):
            lines.append(f"   {i:>4}: {src_line}")
        lines.append("")

    return "\n".join(lines)


def _subdivide_part_groups(
    raw_groups: dict[tuple[Any, ...], list[tuple[dict[str, Any], str]]],
) -> list[tuple[list[tuple[dict[str, Any], str]], list[str]]]:
    entries: list[tuple[list[tuple[dict[str, Any], str]], list[str]]] = []

    for key in sorted(raw_groups.keys()):
        records = raw_groups[key]
        records.sort(
            key=lambda item: (item[0]["start_line"], item[0]["part_index"])
        )

        subgroups: list[list[tuple[dict[str, Any], str]]] = []
        current: list[tuple[dict[str, Any], str]] = []
        prev_idx: int | None = None

        for meta, doc in records:
            if prev_idx is not None and meta["part_index"] <= prev_idx:
                if current:
                    subgroups.append(current)
                current = [(meta, doc)]
            else:
                current.append((meta, doc))
            prev_idx = meta["part_index"]
        if current:
            subgroups.append(current)

        for group in subgroups:
            warnings = _validate_parts(group)
            if len(subgroups) > 1:
                warnings.append(
                    "ambiguous symbol key: multiple definitions share the same "
                    "metadata fields (repo+path+symbol+kind+parent+part_count); "
                    "subdivided by line range"
                )
            entries.append((group, warnings))

    return entries


def get_symbol(
    config: Any,
    *,
    symbol: str,
    repo: str | None = None,
    path: str | None = None,
    kind: str | None = None,
    limit: int | None = None,
) -> str:
    collection = get_collection(
        db_path=config.db, collection_name=config.collection
    )

    conditions: list[dict[str, Any]] = [
        {"chunk_type": {"$in": ["symbol", "symbol_part"]}},
        {"symbol": {"$eq": symbol}},
    ]
    if repo:
        conditions.append({"repo": {"$eq": repo}})
    if path:
        conditions.append({"path": {"$eq": path}})
    if kind:
        conditions.append({"symbol_kind": {"$eq": kind}})

    where: dict[str, Any] = {"$and": conditions}

    payload = collection.get(
        where=where,
        limit=INTERNAL_FETCH_LIMIT,
        include=["documents", "metadatas"],
    )

    documents: list[str] = payload.get("documents") or []
    metadatas: list[dict[str, Any]] = payload.get("metadatas") or []

    if not documents:
        return f"=== Symbol: {symbol} ===\n\n(not found in indexed records)"

    singles: dict[tuple[Any, ...], tuple[dict[str, Any], str]] = {}
    raw_part_groups: dict[
        tuple[Any, ...], list[tuple[dict[str, Any], str]]
    ] = defaultdict(list)

    for meta, doc in zip(metadatas, documents):
        if meta.get("part_index", 0) == 0:
            singles[_single_key(meta)] = (meta, doc)
        else:
            raw_part_groups[_part_group_key(meta)].append((meta, doc))

    part_entries = _subdivide_part_groups(raw_part_groups)

    entries: list[tuple[str, str, int, str]] = []

    for key in sorted(singles.keys()):
        meta, doc = singles[key]
        entries.append(
            (meta["repo"], meta["path"], meta["start_line"], _format_single(meta, doc))
        )

    for group, warnings in part_entries:
        first = group[0][0]
        entries.append(
            (
                first["repo"],
                first["path"],
                first["start_line"],
                _format_multipart(group, warnings),
            )
        )

    entries.sort(key=lambda x: (x[0], x[1], x[2]))

    effective_limit = min(limit, MAX_RESULTS) if limit else MAX_RESULTS
    if len(entries) > effective_limit:
        entries = entries[:effective_limit]
        truncated = f"(results truncated to {effective_limit})"
    else:
        truncated = ""

    header = f"=== Symbol: {symbol} ===\n"
    body = "\n\n---\n\n".join(block for _, _, _, block in entries)
    return header + body + ("\n" + truncated if truncated else "")
