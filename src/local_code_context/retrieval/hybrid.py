from __future__ import annotations

import sys
import time
from typing import Any

from local_code_context.retrieval.lexical import lexical_search
from local_code_context.retrieval.models import (
    HybridCandidate,
    HybridResult,
    candidate_identity,
    composite_identity,
)
from local_code_context.retrieval.query import get_collection, search_chunks
from local_code_context.retrieval.query_intent import classify_query, extract_identifiers
from local_code_context.retrieval.ranking import (
    compute_final_score,
    normalize_exact_symbol,
    normalize_semantic_distance,
    rerank,
)

SEMANTIC_CANDIDATE_LIMIT = 20
LEXICAL_CANDIDATE_LIMIT = 20
MIN_SEMANTIC_SCORE = 0.0
MIN_LEXICAL_SCORE = 0.0
MIN_FINAL_SCORE = 0.0
MAX_RESULT_LIMIT = 20

# Default: sem+symbol only (lexical is strictly opt-in via include_lexical=True).
# Confidence trigger: only fires when semantic search finds essentially nothing
# (top score < 0.02), providing a safety net without adding noise to normal queries.
BOOST_SEMANTIC_THRESHOLD = 0.02
BOOST_SYMBOL_SCORE = 0.5

# If the best raw (pre-bonus) sem+symbol weighted score is below this,
# the query has no relevant content in the index (negative query guard).
NO_RESULT_RAW_THRESHOLD = 0.12


def _semantic_candidates(
    config: Any,
    query: str,
    repo: str | None = None,
) -> list[HybridCandidate]:
    hits = search_chunks(
        db_path=config.db,
        collection_name=config.collection,
        query=query,
        embed_model=config.embed_model,
        ollama_url=config.ollama_url,
        top_k=SEMANTIC_CANDIDATE_LIMIT,
        repo=repo,
    )

    candidates: list[HybridCandidate] = []
    for hit in hits:
        meta: dict[str, Any] = hit["metadata"]
        distance = hit.get("distance")
        score = normalize_semantic_distance(distance)
        if score < MIN_SEMANTIC_SCORE:
            continue
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
            document=hit.get("document", ""),
            semantic_score=score,
            match_sources=["semantic"],
        ))
    return candidates


def _lexical_candidates(
    config: Any,
    query: str,
    repo: str | None = None,
    path: str | None = None,
) -> list[HybridCandidate]:
    return lexical_search(
        config=config,
        query=query,
        repo=repo,
        path=path,
        max_results=LEXICAL_CANDIDATE_LIMIT,
    )


def _exact_symbol_candidates(
    config: Any,
    query: str,
    repo: str | None = None,
    path: str | None = None,
) -> list[HybridCandidate]:
    identifiers = extract_identifiers(query)
    if not identifiers:
        return []

    intent = classify_query(query)
    if intent not in ("exact_symbol", "implementation", "usage", "general"):
        return []

    collection = get_collection(db_path=config.db, collection_name=config.collection)
    candidates: list[HybridCandidate] = []

    for ident in identifiers[:3]:
        conditions: list[dict[str, Any]] = [
            {"chunk_type": {"$in": ["symbol", "symbol_part"]}},
            {"symbol": {"$eq": ident}},
        ]
        if repo:
            conditions.append({"repo": {"$eq": repo}})
        if path:
            conditions.append({"path": {"$eq": path}})

        payload = collection.get(
            where={"$and": conditions},
            limit=50,
            include=["documents", "metadatas"],
        )
        docs = payload.get("documents") or []
        metas = payload.get("metadatas") or []

        for doc, meta in zip(docs, metas):
            query_ident = ident
            symbol = meta.get("symbol", "")
            is_case_exact = symbol == query_ident
            is_case_insensitive = symbol.lower() == query_ident.lower()
            es_score = normalize_exact_symbol(is_case_exact or is_case_insensitive)

            if not is_case_exact and not is_case_insensitive:
                continue

            candidates.append(HybridCandidate(
                record_id=meta.get("id"),
                repo=meta.get("repo", ""),
                path=meta.get("path", ""),
                language=meta.get("language"),
                chunk_type=meta.get("chunk_type"),
                symbol=symbol,
                symbol_kind=meta.get("symbol_kind"),
                parent_symbol=meta.get("parent_symbol"),
                start_line=meta.get("start_line"),
                end_line=meta.get("end_line"),
                part_index=meta.get("part_index"),
                document=doc,
                exact_symbol_score=es_score,
                match_sources=["symbol"],
            ))

    return candidates


def _needs_lexical_boost(
    semantic: list[HybridCandidate],
    symbol: list[HybridCandidate],
) -> bool:
    if not semantic and not symbol:
        return True
    top_sem = max((c.semantic_score for c in semantic), default=0.0)
    top_sym = max((c.exact_symbol_score for c in symbol), default=0.0)
    if top_sem >= BOOST_SEMANTIC_THRESHOLD or top_sym >= BOOST_SYMBOL_SCORE:
        return False
    return True


def _is_likely_negative(
    semantic: list[HybridCandidate],
    symbol: list[HybridCandidate],
) -> bool:
    """Check whether the query has any meaningful sem+symbol match.
    
    Inspects raw pre-bonus scores before lexical noise is added.
    Returns True (likely negative) when no candidate reaches the threshold.
    """
    top_sem = max((c.semantic_score for c in semantic), default=0.0)
    top_sym = max((c.exact_symbol_score for c in symbol), default=0.0)
    weighted = top_sem + 0.4 * top_sym
    return weighted < NO_RESULT_RAW_THRESHOLD


def _merge_candidates(
    *source_lists: list[HybridCandidate],
) -> list[HybridCandidate]:
    by_id: dict[str, HybridCandidate] = {}
    by_composite: dict[tuple[Any, ...], HybridCandidate] = {}

    def _merge(c: HybridCandidate) -> None:
        rid = c.record_id
        if rid and rid in by_id:
            existing = by_id[rid]
            existing.semantic_score = max(existing.semantic_score, c.semantic_score)
            existing.lexical_score = max(existing.lexical_score, c.lexical_score)
            existing.exact_symbol_score = max(
                existing.exact_symbol_score, c.exact_symbol_score
            )
            for src in c.match_sources:
                if src not in existing.match_sources:
                    existing.match_sources.append(src)
            return

        comp = candidate_identity(c)
        if comp in by_composite:
            existing = by_composite[comp]
            existing.semantic_score = max(existing.semantic_score, c.semantic_score)
            existing.lexical_score = max(existing.lexical_score, c.lexical_score)
            existing.exact_symbol_score = max(
                existing.exact_symbol_score, c.exact_symbol_score
            )
            for src in c.match_sources:
                if src not in existing.match_sources:
                    existing.match_sources.append(src)
            return

        if rid:
            by_id[rid] = c
        else:
            by_composite[comp] = c

    for source in source_lists:
        for candidate in source:
            _merge(candidate)

    ordered = sorted(
        list(by_id.values()) + list(by_composite.values()),
        key=lambda c: (
            -c.semantic_score,
            -c.lexical_score,
            -c.exact_symbol_score,
            c.repo,
            c.path,
        ),
    )
    return ordered


def search_code_hybrid(
    config: Any,
    query: str,
    repo: str | None = None,
    path: str | None = None,
    language: str | None = None,
    limit: int = 5,
    semantic_candidate_limit: int | None = None,
    lexical_candidate_limit: int | None = None,
    include_lexical: bool = False,
    _timings: dict[str, float] | None = None,
) -> list[HybridResult]:
    global SEMANTIC_CANDIDATE_LIMIT, LEXICAL_CANDIDATE_LIMIT
    scl = semantic_candidate_limit or SEMANTIC_CANDIDATE_LIMIT
    lcl = lexical_candidate_limit or LEXICAL_CANDIDATE_LIMIT
    SEMANTIC_CANDIDATE_LIMIT = scl
    LEXICAL_CANDIDATE_LIMIT = lcl

    effective_limit = min(limit, MAX_RESULT_LIMIT)

    timings: dict[str, float] = {} if _timings is None else _timings

    # Phase 1: semantic + exact symbol (always)
    t0 = time.time()
    semantic = _semantic_candidates(config, query, repo=repo)
    timings["semantic_candidate_generation"] = (time.time() - t0) * 1000

    if language:
        semantic = [c for c in semantic if c.language == language]

    t0 = time.time()
    symbol = _exact_symbol_candidates(config, query, repo=repo, path=path)
    timings["exact_symbol_lookup"] = (time.time() - t0) * 1000

    if language:
        symbol = [c for c in symbol if c.language == language]

    # Phase 2: pre-lexical negative-query guard (checks raw sem+symbol scores)
    if _is_likely_negative(semantic, symbol):
        timings["lexical_candidate_generation"] = 0.0
        timings["merge_and_dedup"] = 0.0
        timings["reranking"] = 0.0
        return []

    # Phase 3: lexical (opt-in or confidence-triggered)
    t0 = time.time()
    lexical: list[HybridCandidate] = []
    if include_lexical or _needs_lexical_boost(semantic, symbol):
        lexical = _lexical_candidates(config, query, repo=repo, path=path)
        if language:
            lexical = [c for c in lexical if c.language == language]
    timings["lexical_candidate_generation"] = (time.time() - t0) * 1000

    # Phase 4: merge + dedup + rerank
    t0 = time.time()
    merged = _merge_candidates(semantic, lexical, symbol)
    timings["merge_and_dedup"] = (time.time() - t0) * 1000

    t0 = time.time()
    results = rerank(merged, effective_limit)
    timings["reranking"] = (time.time() - t0) * 1000

    return results
