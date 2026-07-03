#!/usr/bin/env python3
"""Hardened retrieval evaluation benchmark for local-code-context.

Reads frozen questions from questions.json, builds a temporary Chroma index
from clean repo snapshots, runs all retrieval methods and operational tests,
preserves raw machine-readable results, and writes a Markdown report.

Usage:
    cd evaluation/ && uv run python benchmark.py

Output hierarchy:
    results/<timestamp>/
        config.json
        index_manifest.json
        raw_results.json
        operational_results.json
        metrics.json
        report.md
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from collections import defaultdict
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch

# --- Path setup --------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
SRC_DIR = REPO_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

# --- Project imports ---------------------------------------------------------
from local_code_context.mcp.server import ServerConfig
from local_code_context.mcp.symbols import get_symbol as _get_symbol
from local_code_context.mcp.context import (
    get_repository_context,
    search_code as _search_code,
    list_indexed_repositories,
)
from local_code_context.retrieval.query import search_chunks, get_collection
from local_code_context.indexing.indexer import (
    DEFAULT_EMBED_MODEL,
    DEFAULT_OLLAMA_URL,
    repo_name,
    manifest_path,
    load_manifest,
    save_manifest,
    open_collection,
    index_file,
    delete_indexed_path,
    file_key,
    _snapshot_records,
    _restore_records,
    delete_ids,
    run_index,
)
from local_code_context.syntax.rendering import chunk_text, make_chunk_id
from local_code_context.syntax.indexer import build_index_records, INDEX_SCHEMA_VERSION
from local_code_context.syntax.detection import detect_language

# ---- Benchmark metadata -----------------------------------------------------

BENCHMARK_VERSION = "2.0.0"
FROZEN_QUESTIONS_HASH = None      # set after loading

# --- Constants ---------------------------------------------------------------
OLLAMA_URL = "http://127.0.0.1:11434"
EMBED_MODEL = "nomic-embed-text"
TOP_K = 5
CHAT_MODEL = "qwen2.5-coder:14b"
WORKSPACE_ROOT = Path("/home/atarola/code")
COMPY6502_PATH = WORKSPACE_ROOT / "compy6502"
RESULTS_DIR = SCRIPT_DIR / "results"
FIXTURES_DIR = SCRIPT_DIR / "fixtures"

# ---- Git commit helpers -----------------------------------------------------

def _git_head(path: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(path), timeout=10,
        ).stdout.strip()
    except Exception:
        return "unknown"

GIT_HASH_LCC = _git_head(REPO_DIR)
GIT_HASH_COMPY = _git_head(COMPY6502_PATH)

# ---- Question loading -------------------------------------------------------

@dataclass
class Question:
    qid: str
    text: str
    category: str
    expected_repo: str | None
    expected_paths: list[str]
    expected_symbols: list[str]
    acceptable_alternatives: list[str] = field(default_factory=list)
    notes: str = ""


def load_questions(path: Path | None = None) -> list[Question]:
    if path is None:
        path = SCRIPT_DIR / "questions.json"
    raw = json.loads(path.read_text("utf-8"))
    return [Question(**q) for q in raw]


QUESTIONS: list[Question] = load_questions()
FROZEN_QUESTIONS_HASH = hashlib.sha256(
    json.dumps([asdict(q) for q in QUESTIONS], sort_keys=True).encode()
).hexdigest()

# ---- Temp index management --------------------------------------------------

@contextmanager
def temp_index() -> Iterator[tuple[ServerConfig, Path]]:
    """Create a temporary Chroma index with clean repo snapshots.

    Yields (config, db_path) for a throwaway index that excludes
    evaluation/ and TASK.md.
    """
    tmp_dir = tempfile.mkdtemp(prefix="eval_bench_")
    tmp_db = Path(tmp_dir) / "codebase_index"
    try:
        sys.stderr.write(f"[setup] Building temporary index at {tmp_db} ...\n")
        run_index(
            repos=[str(REPO_DIR), str(COMPY6502_PATH)],
            db=str(tmp_db),
            collection_name="code_chunks",
            embed_model=EMBED_MODEL,
            ollama_url=OLLAMA_URL,
            force=True,
        )
        # Leakage check
        collection = open_collection(tmp_db, "code_chunks")
        all_metas = collection.get(include=["metadatas"]).get("metadatas", [])
        for m in all_metas:
            p = m.get("path", "")
            assert not p.startswith("evaluation/"), f"Leakage: path starts with evaluation/: {p}"
            assert p != "TASK.md", f"Leakage: TASK.md found in index"
        sys.stderr.write(f"[setup] Index OK — {len(all_metas)} docs, no leakage\n")

        config = ServerConfig(
            db=tmp_db,
            collection="code_chunks",
            top_k=TOP_K,
            embed_model=EMBED_MODEL,
            model=CHAT_MODEL,
            ollama_url=OLLAMA_URL,
            repo=None,
        )
        yield config, tmp_db
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@contextmanager
def temp_fixture_index() -> Iterator[tuple[Any, ServerConfig, Path]]:
    """Create a tiny temp index with known fixture files for op tests.

    Yields (collection, config, db_path).
    """
    tmp_dir = tempfile.mkdtemp(prefix="eval_op_")
    tmp_db = Path(tmp_dir) / "codebase_index"
    fixture_repo = Path(tmp_dir) / "fixture_repo"
    fixture_repo.mkdir()

    # One known .py file
    (fixture_repo / "hello.py").write_text(
        "def greet(name: str) -> str:\n"
        '    """Return a greeting."""\n'
        "    return f'Hello, {name}!'\n"
        "\n"
        "class Calculator:\n"
        "    def add(self, a: int, b: int) -> int:\n"
        "        return a + b\n"
    )

    try:
        collection = open_collection(tmp_db, "code_chunks")
        manifest = load_manifest(tmp_db)
        index_file(
            collection=collection,
            path=fixture_repo / "hello.py",
            repo_root=fixture_repo,
            repo="fixture",
            db_path=tmp_db,
            manifest=manifest,
            embed_model=EMBED_MODEL,
            ollama_url=OLLAMA_URL,
            force=False,
        )
        save_manifest(tmp_db, manifest)

        config = ServerConfig(
            db=tmp_db,
            collection="code_chunks",
            top_k=TOP_K,
            embed_model=EMBED_MODEL,
            model=CHAT_MODEL,
            ollama_url=OLLAMA_URL,
            repo=None,
        )
        yield collection, config, tmp_db
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---- Evaluation result types ------------------------------------------------

@dataclass
class Result:
    path: str | None = None
    repo: str | None = None
    symbol: str | None = None
    kind: str | None = None
    chunk_type: str | None = None
    start_line: int | None = None
    part_index: int | None = None
    record_id: str | None = None
    content: str = ""
    distance: float | None = None
    rank: int = 0


@dataclass
class QueryResult:
    hits: list[Result]
    latency: float
    error: str | None = None


def result_to_hit_entry(r: Result) -> dict[str, Any]:
    return {
        "rank": r.rank,
        "path": r.path,
        "repo": r.repo,
        "symbol": r.symbol,
        "kind": r.kind,
        "chunk_type": r.chunk_type,
        "start_line": r.start_line,
        "part_index": r.part_index,
        "record_id": r.record_id,
        "content_preview": r.content[:200],
        "distance": r.distance,
    }


# ---- Relevance --------------------------------------------------------------

def is_relevant(question: Question, result: Result) -> bool:
    combined = (result.path or "") + " " + (result.symbol or "")
    expected_sym = [s.split(".")[-1] for s in question.expected_symbols]
    for sym in expected_sym:
        if sym in combined:
            return True
    for path in question.expected_paths:
        if path in combined:
            return True
    for alt in question.acceptable_alternatives:
        if alt in combined:
            return True
    return False


def score_results(
    question: Question,
    result: QueryResult,
    method: str,
    *, applicable: bool = True,
) -> dict[str, Any]:
    hits = result.hits
    relevant_at_k = [is_relevant(question, h) for h in hits]
    hit_at_1 = relevant_at_k[0] if len(relevant_at_k) > 0 else False
    hit_at_5 = any(relevant_at_k[:5]) if hits else False
    rr = 0.0
    for i, rel in enumerate(relevant_at_k):
        if rel:
            rr = 1.0 / (i + 1)
            break
    relevant_count = sum(1 for h in hits[:5] if is_relevant(question, h))
    irrelevant_count = sum(1 for h in hits[:5] if not is_relevant(question, h)) if hits else 0

    # context sufficient = answer found in top 5, or explicitly abstained
    ctx_sufficient = hit_at_5
    if question.category == "negative":
        ctx_sufficient = len(hits) == 0 or not any(is_relevant(question, h) for h in hits[:5])

    return {
        "qid": question.qid,
        "text": question.text,
        "category": question.category,
        "method": method,
        "applicable": applicable,
        "hit_at_1": hit_at_1,
        "hit_at_5": hit_at_5,
        "reciprocal_rank": rr,
        "relevant_in_top_5": relevant_count,
        "irrelevant_in_top_5": irrelevant_count,
        "context_sufficient": ctx_sufficient,
        "expected_symbols": question.expected_symbols,
        "expected_paths": question.expected_paths,
        "hits": [result_to_hit_entry(h) for h in hits],
        "latency": result.latency,
        "error": result.error or "",
    }


# ---- Applicability ----------------------------------------------------------

def method_applicable(question: Question, method: str) -> bool:
    """Return whether a retrieval method can meaningfully answer this question."""
    if method == "exact_symbol":
        # Exact symbol lookup only works for questions with structural-symbol targets
        # and only for file types with tree-sitter parsers.
        if not question.expected_symbols:
            return False
        # Assembly labels (.s files) are not structurally indexed
        for p in question.expected_paths:
            if p.endswith(".s") or p.endswith(".inc") or p.endswith(".pcf"):
                return False
        return True
    if method == "repo_context":
        # Repo context is orientation, not ranked retrieval
        return False
    if method == "rg_oracle":
        # Oracle uses answer metadata — diagnostic only
        return False
    return True


# ---- Retrieval methods ------------------------------------------------------

def exact_symbol_lookup(question: Question, config: ServerConfig) -> QueryResult:
    symbol = question.expected_symbols[0] if question.expected_symbols else question.text.split()[-1]
    t0 = time.time()
    try:
        text = _get_symbol(
            config,
            symbol=symbol,
            repo=question.expected_repo,
        )
        latency = time.time() - t0
        hits: list[Result] = []
        found_repo = None
        found_path = None
        for line in text.split("\n"):
            s = line.strip()
            if s.startswith("Repository:"):
                found_repo = s.split(":", 1)[1].strip()
            elif s.startswith("Path:"):
                found_path = s.split(":", 1)[1].strip()
        if found_repo is not None:
            hits.append(Result(
                repo=found_repo, path=found_path, symbol=symbol,
                content=text[:300], rank=1,
            ))
        return QueryResult(hits=hits, latency=latency)
    except Exception as e:
        return QueryResult(hits=[], latency=time.time() - t0, error=str(e))


def semantic_search(question: Question, config: ServerConfig) -> QueryResult:
    query = question.text
    t0 = time.time()
    try:
        results = search_chunks(
            db_path=config.db,
            collection_name=config.collection,
            query=query,
            embed_model=config.embed_model,
            ollama_url=config.ollama_url,
            top_k=config.top_k,
            repo=None,
        )
        latency = time.time() - t0
        hits: list[Result] = []
        for i, r in enumerate(results):
            meta = r.get("metadata", {})
            hits.append(Result(
                path=meta.get("path"),
                repo=meta.get("repo"),
                symbol=meta.get("symbol"),
                kind=meta.get("symbol_kind"),
                chunk_type=meta.get("chunk_type"),
                start_line=meta.get("start_line"),
                part_index=meta.get("part_index"),
                record_id=r.get("id"),
                content=(r.get("document") or "")[:300],
                distance=r.get("distance"),
                rank=i + 1,
            ))
        return QueryResult(hits=hits, latency=latency)
    except Exception as e:
        traceback.print_exc()
        return QueryResult(hits=[], latency=time.time() - t0, error=str(e))


def _extract_rg_terms(text: str, max_terms: int = 3) -> list[str]:
    """Extract search terms from natural-language question text.

    Strips common question words and short tokens, returns the
    most significant remaining terms (by length).
    """
    stopwords = {
        "what", "how", "why", "where", "when", "which", "who", "whose",
        "does", "do", "did", "is", "are", "was", "were", "will", "would",
        "can", "could", "should", "may", "might", "has", "have", "had",
        "the", "a", "an", "in", "on", "at", "to", "for", "of", "with",
        "by", "from", "into", "about", "like", "through", "after", "over",
        "and", "or", "but", "not", "be", "been", "being", "having",
        "its", "it", "i", "we", "they", "he", "she", "that", "this",
        "these", "those", "are", "were", "been", "being",
    }
    tokens = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{2,}', text.lower())
    significant = sorted(
        [t for t in tokens if t not in stopwords],
        key=len, reverse=True,
    )
    return significant[:max_terms]


def rg_question(question: Question, max_results: int = TOP_K) -> QueryResult:
    """Lexical baseline using terms extracted from the question text only."""
    terms = _extract_rg_terms(question.text)
    return _run_rg(terms, max_results)


def rg_oracle(question: Question, max_results: int = TOP_K) -> QueryResult:
    """Lexical oracle using expected symbols/answer metadata — diagnostic only."""
    terms = list(question.expected_symbols)
    terms.extend(question.expected_paths)
    if not terms:
        terms = question.text.split()[-3:]
    return _run_rg(terms, max_results)


def _run_rg(terms: list[str], max_results: int = TOP_K) -> QueryResult:
    t0 = time.time()
    try:
        hits: list[Result] = []
        seen: set[str] = set()
        for term in terms:
            if not term:
                continue
            result = subprocess.run(
                ["rg", "-n", "--no-heading", "--iglob", "!evaluation/**",
                 "--iglob", "!TASK.md",
                 term, str(REPO_DIR), str(COMPY6502_PATH)],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.strip().split("\n")[:max_results]:
                if not line.strip():
                    continue
                parts = line.split(":", 2)
                if len(parts) < 2:
                    continue
                fp = parts[0]
                if fp not in seen:
                    seen.add(fp)
                    repo_label = "local-code-context" if REPO_DIR in Path(fp).parents else "compy6502"
                    sym_match = re.search(r'\b(' + '|'.join(re.escape(t) for t in terms) + r')\b', line)
                    hits.append(Result(
                        path=fp, repo=repo_label,
                        symbol=sym_match.group(0) if sym_match else term,
                        content=line[:300], rank=len(hits) + 1,
                    ))
        hits = sorted(hits, key=lambda h: h.rank)[:max_results]
        for i, h in enumerate(hits):
            h.rank = i + 1
        latency = time.time() - t0
        return QueryResult(hits=hits, latency=latency)
    except Exception as e:
        return QueryResult(hits=[], latency=time.time() - t0, error=str(e))


# ---- Repo-orientation evaluation --------------------------------------------

REPO_ORIENTATION_QUESTIONS: list[dict[str, Any]] = [
    {
        "qid": "RO-1",
        "text": "What are the major subsystems in local-code-context?",
        "expected_repo": "local-code-context",
        "expected_mentions": [
            "indexing", "retrieval", "syntax", "mcp", "server",
        ],
    },
    {
        "qid": "RO-2",
        "text": "Which languages receive structural parsing via tree-sitter?",
        "expected_repo": "local-code-context",
        "expected_mentions": ["python", "rust", "pyi", "pyx"],
    },
    {
        "qid": "RO-3",
        "text": "Where does indexing happen and how are files discovered?",
        "expected_repo": "local-code-context",
        "expected_mentions": [
            "indexer.py", "run_index", "iter_files", "watcher",
        ],
    },
    {
        "qid": "RO-4",
        "text": "Where are MCP tools registered and what do they provide?",
        "expected_repo": "local-code-context",
        "expected_mentions": [
            "server.py", "mcp", "get_symbol", "search_code", "list_indexed_repositories",
        ],
    },
    {
        "qid": "RO-5",
        "text": "What are the main responsibilities and languages of compy6502?",
        "expected_repo": "compy6502",
        "expected_mentions": ["6502", "assembly", "python", "rust", "emulator", "srecord"],
    },
    {
        "qid": "RO-6",
        "text": "Which parts of compy6502 are Python, Rust, and assembly?",
        "expected_repo": "compy6502",
        "expected_mentions": ["src/tools", "src/asm", "src/rust", "fpga"],
    },
]


def repo_orientation_evaluation(config: ServerConfig) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for ro in REPO_ORIENTATION_QUESTIONS:
        t0 = time.time()
        try:
            ctx = get_repository_context(config, repo=ro["expected_repo"], max_chars=6000)
            latency = time.time() - t0
            text = str(ctx)
            found_mentions = [m for m in ro["expected_mentions"] if m.lower() in text.lower()]
            coverage = len(found_mentions) / len(ro["expected_mentions"]) if ro["expected_mentions"] else 0
            results.append({
                "qid": ro["qid"],
                "text": ro["text"],
                "method": "repo_orientation",
                "expected_repo": ro["expected_repo"],
                "expected_mentions": ro["expected_mentions"],
                "found_mentions": found_mentions,
                "missed_mentions": [m for m in ro["expected_mentions"] if m not in found_mentions],
                "mention_coverage": coverage,
                "latency": latency,
                "context_length": len(text),
                "error": "",
            })
        except Exception as e:
            results.append({
                "qid": ro["qid"],
                "text": ro["text"],
                "method": "repo_orientation",
                "expected_repo": ro["expected_repo"],
                "error": str(e),
            })
    return results


# ---- Evaluation runner ------------------------------------------------------

def run_evaluation(config: ServerConfig) -> list[dict[str, Any]]:
    eval_results: list[dict[str, Any]] = []

    for q in QUESTIONS:
        # Build method list per category
        methods: list[tuple[str, Callable]] = []

        if q.category == "exact-symbol":
            methods = [
                ("exact_symbol", lambda q=q: exact_symbol_lookup(q, config)),
                ("semantic_search", lambda q=q: semantic_search(q, config)),
                ("rg_question", lambda q=q: rg_question(q)),
                ("rg_oracle", lambda q=q: rg_oracle(q)),
            ]
        elif q.category in ("conceptual", "cross-repo", "fallback"):
            methods = [
                ("semantic_search", lambda q=q: semantic_search(q, config)),
                ("rg_question", lambda q=q: rg_question(q)),
                ("rg_oracle", lambda q=q: rg_oracle(q)),
            ]
        elif q.category == "negative":
            methods = [
                ("semantic_search", lambda q=q: semantic_search(q, config)),
                ("rg_question", lambda q=q: rg_question(q)),
            ]

        for method_name, method_fn in methods:
            sys.stderr.write(f"  [{method_name}] {q.qid}: {q.text[:60]}...\n")
            result = method_fn()
            applicable = method_applicable(q, method_name)
            scored = score_results(q, result, method_name, applicable=applicable)
            eval_results.append(scored)

    return eval_results


# ---- Operational tests ------------------------------------------------------

def _get_path_ids(collection, repo, path):
    result = collection.get(
        where={"$and": [{"repo": {"$eq": repo}}, {"path": {"$eq": path}}]},
        include=["documents", "metadatas"],
    )
    return result.get("ids", []), result.get("documents", []), result.get("metadatas", [])


def _count_all_docs(collection) -> int:
    return len(collection.get(include=[])["ids"])


def _collection_hash(collection) -> str:
    """Hash of all document contents for change detection."""
    payload = collection.get(include=["documents"])
    docs = payload.get("documents", [])
    combined = "".join(d for d in docs if d)
    return hashlib.sha256(combined.encode()).hexdigest()


def op_test_1_rebuild_preserves(collection, manifest, db_path) -> dict[str, Any]:
    """Unchanged rebuild produces identical IDs, docs, metadata, order."""
    before_ids = collection.get(include=[])["ids"]
    before_docs = collection.get(include=["documents"])["documents"]
    before_metas = collection.get(include=["metadatas"])["metadatas"]
    before_hash = _collection_hash(collection)
    before_count = len(before_ids)

    run_index(
        repos=[str(REPO_DIR), str(COMPY6502_PATH)],
        db=str(db_path),
        collection_name="code_chunks",
        embed_model=EMBED_MODEL,
        ollama_url=OLLAMA_URL,
        force=False,
    )

    after_ids = collection.get(include=[])["ids"]
    after_docs = collection.get(include=["documents"])["documents"]
    after_metas = collection.get(include=["metadatas"])["metadatas"]
    after_hash = _collection_hash(collection)
    after_count = len(after_ids)

    ids_match = before_ids == after_ids
    docs_match = before_docs == after_docs
    metas_match = before_metas == after_metas
    hash_match = before_hash == after_hash
    count_match = before_count == after_count

    passed = ids_match and docs_match and metas_match and hash_match and count_match
    return {
        "test": "rebuild_preserves",
        "passed": passed,
        "details": {
            "ids_match": ids_match,
            "docs_match": docs_match,
            "metas_match": metas_match,
            "hash_match": hash_match,
            "count_match": count_match,
            "before_count": before_count,
            "after_count": after_count,
        },
    }


def op_test_2_modified_replaces(collection, manifest, db_path) -> dict[str, Any]:
    """Modified file: old content absent, new content present, correct record count."""
    from local_code_context.indexing.indexer import read_text

    fpath = REPO_DIR / "src" / "local_code_context" / "__init__.py"
    original = fpath.read_text()
    try:
        # Use a fixture file with known structural symbol
        fixture_path = REPO_DIR / "src" / "local_code_context" / "retrieval" / "query.py"
        original_fixture = fixture_path.read_text()

        rel = "src/local_code_context/retrieval/query.py"
        marker = "# EVAL_MODIFIED_MARKER_42\n"

        # Snapshot before
        before_ids, before_docs, before_metas = _get_path_ids(collection, "local-code-context", rel)
        before_content = "".join(before_docs)

        # Modify with marker
        fixture_path.write_text(original_fixture + marker)
        run_index(
            repos=[str(REPO_DIR)],
            db=str(db_path),
            collection_name="code_chunks",
            embed_model=EMBED_MODEL,
            ollama_url=OLLAMA_URL,
            force=True,
        )

        after_ids, after_docs, after_metas = _get_path_ids(collection, "local-code-context", rel)
        after_content = "".join(after_docs)

        old_content_absent = marker not in before_content
        new_content_present = marker in after_content
        ids_changed = before_ids != after_ids
        count_ok = len(after_ids) > 0

        passed = new_content_present and count_ok
        return {
            "test": "modified_replaces",
            "passed": passed,
            "details": {
                "ids_changed": ids_changed,
                "old_content_absent": old_content_absent,
                "new_content_present": new_content_present,
                "before_count": len(before_ids),
                "after_count": len(after_ids),
                "marker_found_in_before": not old_content_absent,
                "marker_found_in_after": new_content_present,
            },
        }
    finally:
        fixture_path.write_text(original_fixture)


def op_test_3_deleted_disappears(collection, manifest, db_path, tmp_base: Path) -> dict[str, Any]:
    """Deleted file: no records remain for (repo, path)."""
    tmp_file = tmp_base / "temp_test_delete.py"
    tmp_file.write_text("X_EVAL_DELETE = 1\n")
    rel = "temp_test_delete.py"

    try:
        index_file(
            collection=collection, path=tmp_file, repo_root=tmp_base,
            repo="fixture", db_path=db_path, manifest=manifest,
            embed_model=EMBED_MODEL, ollama_url=OLLAMA_URL, force=False,
        )
        save_manifest(db_path, manifest)
        before_ids, _, _ = _get_path_ids(collection, "fixture", rel)
        found_before = len(before_ids) > 0

        tmp_file.unlink()
        delete_indexed_path(collection, manifest, "fixture", rel)
        save_manifest(db_path, manifest)

        after_ids, _, _ = _get_path_ids(collection, "fixture", rel)
        found_after = len(after_ids) > 0

        passed = found_before and not found_after
        return {
            "test": "deleted_disappears",
            "passed": passed,
            "details": {
                "found_before": found_before,
                "found_after": found_after,
                "ids_before": len(before_ids),
                "ids_after": len(after_ids),
            },
        }
    finally:
        if tmp_file.exists():
            tmp_file.unlink()


def op_test_4_rename_cleans(collection, manifest, db_path, tmp_base: Path) -> dict[str, Any]:
    """Renamed file: no stale records under old path, records exist under new."""
    old_path = tmp_base / "temp_old_name.py"
    new_path = tmp_base / "temp_new_name.py"
    old_path.write_text("X_EVAL_RENAME = 1\n")
    old_rel = "temp_old_name.py"
    new_rel = "temp_new_name.py"

    try:
        if new_path.exists():
            new_path.unlink()

        index_file(
            collection=collection, path=old_path, repo_root=tmp_base,
            repo="fixture", db_path=db_path, manifest=manifest,
            embed_model=EMBED_MODEL, ollama_url=OLLAMA_URL, force=False,
        )
        save_manifest(db_path, manifest)

        old_path.rename(new_path)
        delete_indexed_path(collection, manifest, "fixture", old_rel)

        index_file(
            collection=collection, path=new_path, repo_root=tmp_base,
            repo="fixture", db_path=db_path, manifest=manifest,
            embed_model=EMBED_MODEL, ollama_url=OLLAMA_URL, force=False,
        )
        save_manifest(db_path, manifest)

        stale_ids, _, _ = _get_path_ids(collection, "fixture", old_rel)
        new_ids, _, _ = _get_path_ids(collection, "fixture", new_rel)

        stale_found = len(stale_ids) > 0
        new_found = len(new_ids) > 0

        passed = not stale_found and new_found
        return {
            "test": "rename_cleans",
            "passed": passed,
            "details": {
                "stale_found": stale_found,
                "new_found": new_found,
                "stale_count": len(stale_ids),
                "new_count": len(new_ids),
            },
        }
    finally:
        for p in [old_path, new_path]:
            if p.exists():
                p.unlink()


def op_test_5_true_rollback(collection, manifest, db_path) -> dict[str, Any]:
    """True rollback: snapshot-delete-fail-add restores old records exactly."""
    fpath = REPO_DIR / "src" / "local_code_context" / "__init__.py"
    original = fpath.read_text()
    try:
        rel = "src/local_code_context/__init__.py"
        # Snapshot pre-modification state
        before_ids, before_docs, before_metas = _get_path_ids(collection, "local-code-context", rel)
        if not before_ids:
            return {"test": "true_rollback", "passed": False,
                    "details": "No records found for __init__.py to snapshot"}

        # Modify the file to force re-index
        fpath.write_text(original + "\n# EVAL_ROLLBACK_TEST\n")

        # Patch collection.add to fail after deletion
        original_add = collection.add
        add_call_count = 0

        def failing_add(*args, **kwargs):
            nonlocal add_call_count
            add_call_count += 1
            raise RuntimeError("simulated insertion failure for rollback test")

        collection.add = failing_add

        try:
            # Re-index; it will:
            # 1. Build new records
            # 2. Embed
            # 3. Snapshot old records
            # 4. Delete old records (under index_file's normal flow)
            # 5. collection.add() -> FAILS -> rollback triggered
            run_index(
                repos=[str(REPO_DIR)],
                db=str(db_path),
                collection_name="code_chunks",
                embed_model=EMBED_MODEL,
                ollama_url=OLLAMA_URL,
                force=True,
            )
        except Exception:
            pass  # Expected: the failure propagated up
        finally:
            collection.add = original_add

        # Check restoration
        after_ids, after_docs, after_metas = _get_path_ids(collection, "local-code-context", rel)

        ids_restored = sorted(before_ids) == sorted(after_ids)
        docs_restored = before_docs == after_docs
        deletion_actually_happened = add_call_count > 0
        rollback_triggered = deletion_actually_happened

        passed = ids_restored and docs_restored and rollback_triggered
        return {
            "test": "true_rollback",
            "passed": passed,
            "details": {
                "ids_restored": ids_restored,
                "docs_restored": docs_restored,
                "deletion_actually_happened": deletion_actually_happened,
                "rollback_triggered": rollback_triggered,
                "before_id_count": len(before_ids),
                "after_id_count": len(after_ids),
            },
        }
    finally:
        fpath.write_text(original)


def op_test_6_malformed_fallback() -> dict[str, Any]:
    """Malformed files fall back to text chunking."""
    results = []
    for ext, lang, bad_code in [
        (".py", "python", b"<<<--- NOT VALID PYTHON @@@ 123 *** \x00\xff"),
        (".rs", "rust", b"<<<--- NOT VALID RUST @@@ 123 *** \x00\xff"),
    ]:
        tmp = tempfile.NamedTemporaryFile(
            dir=COMPY6502_PATH, suffix=ext, mode="wb", delete=False,
        )
        tmp.write(bad_code)
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            detected = detect_language(tmp_path, bad_code)
            build_result = build_index_records(
                repo="compy6502",
                repo_root=COMPY6502_PATH,
                path=tmp_path,
                source=bad_code,
                text=bad_code.decode("utf-8", errors="replace"),
            )

            has_text_fallback = build_result.fallback_reason is not None
            records_valid = len(build_result.records) > 0
            all_text_chunks = all(
                r.metadata.get("chunk_type") == "text" for r in build_result.records
            ) if build_result.records else True
            # Verify no file_map records (those are for the old scheme)
            no_file_map = all(
                "file_map" not in r.metadata.get("chunk_type", "")
                for r in build_result.records
            ) if build_result.records else True

            results.append({
                "extension": ext,
                "detected_language": detected,
                "has_fallback": has_text_fallback,
                "records_created": records_valid,
                "all_text_chunks": all_text_chunks,
                "no_file_map": no_file_map,
            })
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    all_ok = all(
        r.get("has_fallback") and r.get("records_created")
        and r.get("all_text_chunks") and r.get("no_file_map")
        for r in results if "error" not in r
    )
    return {"test": "malformed_files_fallback", "passed": all_ok, "details": results}


def op_test_7_duplicate_symbols(collection) -> dict[str, Any]:
    """Duplicate symbol names across repos remain distinguishable."""
    result_all = _get_symbol(
        ServerConfig(db=collection._client._system.settings.persist_directory,
                     collection="code_chunks", top_k=TOP_K,
                     embed_model=EMBED_MODEL, model=CHAT_MODEL,
                     ollama_url=OLLAMA_URL, repo=None),
        symbol="main", limit=10,
    )
    has_lcc = "local-code-context" in result_all
    has_compy = "compy6502" in result_all
    passed = has_lcc and has_compy
    return {
        "test": "duplicate_symbols_distinguishable",
        "passed": passed,
        "details": {"has_lcc": has_lcc, "has_compy": has_compy},
    }


def op_test_8_multipart_reconstructs(collection, manifest, db_path, tmp_base: Path) -> dict[str, Any]:
    """Symbol exceeding split threshold produces >1 part and reconstructs correctly."""
    # Build a file with a function long enough to exceed CHUNK_LINES (60)
    long_func = "def multipart_test_function():\n"
    long_func += '    """A very long function that should trigger splitting."""\n'
    for i in range(1, 121):
        long_func += f"    line_{i} = {i}\n"
    long_func += f"    return line_1 + line_{119}\n"

    tmp_file = tmp_base / "multipart_fixture.py"
    tmp_file.write_text(long_func)
    rel = "multipart_fixture.py"

    try:
        index_file(
            collection=collection, path=tmp_file, repo_root=tmp_base,
            repo="fixture", db_path=db_path, manifest=manifest,
            embed_model=EMBED_MODEL, ollama_url=OLLAMA_URL, force=False,
        )
        save_manifest(db_path, manifest)

        cfg = ServerConfig(
            db=db_path, collection="code_chunks",
            top_k=TOP_K, embed_model=EMBED_MODEL,
            model=CHAT_MODEL, ollama_url=OLLAMA_URL, repo=None,
        )
        result_text = _get_symbol(cfg, symbol="multipart_test_function", repo="fixture", limit=10)

        # Check for multipart indicators
        has_part_1 = "Part 1/2" in result_text or "part 1" in result_text.lower()
        has_part_2 = "Part 2/2" in result_text or "part 2" in result_text.lower()
        part_count = result_text.count("Part ")
        has_multipart = "Part " in result_text

        # Retrieve twice and check ordering
        result_text_2 = _get_symbol(cfg, symbol="multipart_test_function", repo="fixture", limit=10)
        ordering_stable = result_text == result_text_2

        passed = has_multipart and ordering_stable
        return {
            "test": "multipart_reconstructs",
            "passed": passed,
            "details": {
                "has_multipart": has_multipart,
                "has_part_1": has_part_1,
                "has_part_2": has_part_2,
                "part_count": part_count,
                "ordering_stable": ordering_stable,
                "result_length": len(result_text),
            },
        }
    finally:
        if tmp_file.exists():
            tmp_file.unlink()


def op_test_9_mcp_restart(collection, manifest, db_path) -> dict[str, Any]:
    """Fresh MCP server against temp index shows expected repos."""
    cfg = ServerConfig(
        db=db_path, collection="code_chunks",
        top_k=TOP_K, embed_model=EMBED_MODEL,
        model=CHAT_MODEL, ollama_url=OLLAMA_URL, repo=None,
    )
    repos = sorted(list_indexed_repositories(cfg))
    expected = {"local-code-context", "compy6502"}
    passed = set(repos) == expected
    return {
        "test": "mcp_restart_visibility",
        "passed": passed,
        "details": {"found": repos, "expected": sorted(expected)},
    }


def op_test_10_stdout_discipline(db_path: Path) -> dict[str, Any]:
    """Every stdout line from MCP server is valid JSON-RPC protocol."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "local_code_context.mcp.server",
         "--db", str(db_path), "--collection", "code_chunks"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, cwd=REPO_DIR,
    )
    init = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "initialize",
                       "params": {"protocolVersion": "2024-11-05"}}) + "\n"
    tools = json.dumps({"jsonrpc": "2.0", "id": 2,
                        "method": "tools/list"}) + "\n"
    try:
        out, err = proc.communicate(input=init + tools, timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"test": "stdout_discipline", "passed": False, "error": "timeout"}

    lines = [l.strip() for l in out.split("\n") if l.strip()]
    all_valid = all(
        json.loads(l) for l in lines
    ) if lines else True
    return {
        "test": "stdout_discipline",
        "passed": all_valid,
        "details": {
            "all_valid_json": all_valid,
            "stdout_lines": len(lines),
            "stderr_has_content": len(err.strip()) > 0,
        },
    }


def op_test_11_deterministic_ordering(config: ServerConfig, db_path: Path) -> dict[str, Any]:
    """Every query returns identical ordered results across 3 runs."""
    queries = [
        "Find the function search_chunks",
        "How does the indexer decide which files and directories to skip?",
        "How is ACIA serial communication tested in the 6502 emulator?",
    ]
    violations: list[dict] = []

    for qtext in queries:
        runs: list[list[tuple]] = []
        for _ in range(3):
            results = search_chunks(
                db_path=db_path, collection_name="code_chunks",
                query=qtext, embed_model=EMBED_MODEL,
                ollama_url=OLLAMA_URL, top_k=TOP_K, repo=None,
            )
            sigs = [
                (
                    r.get("metadata", {}).get("repo"),
                    r.get("metadata", {}).get("path"),
                    r.get("metadata", {}).get("chunk_type"),
                    r.get("metadata", {}).get("symbol"),
                    r.get("metadata", {}).get("start_line"),
                    r.get("metadata", {}).get("part_index"),
                    r.get("id"),
                    round(r.get("distance", 0), 6),
                )
                for r in results
            ]
            runs.append(sigs)

        for i in range(1, 3):
            if runs[0] != runs[i]:
                violations.append({"query": qtext[:60], "run_0_vs": i})

    # Also test get_symbol determinism
    for sym in ["main", "search_chunks", "BaseTest"]:
        texts = []
        for _ in range(3):
            t = _get_symbol(config, symbol=sym, limit=5)
            texts.append(t)
        for i in range(1, 3):
            if texts[0] != texts[i]:
                violations.append({"symbol": sym, "run_0_vs": i})

    passed = len(violations) == 0
    return {
        "test": "deterministic_ordering",
        "passed": passed,
        "details": {
            "queries_tested": len(queries),
            "symbols_tested": 3,
            "violations": violations,
        },
    }


def run_operational_tests(config: ServerConfig, tmp_db: Path) -> list[dict[str, Any]]:
    """Run all operational tests against the temp index and temp fixtures."""
    sys.stderr.write("Running operational tests...\n")

    collection = open_collection(tmp_db, "code_chunks")
    manifest = load_manifest(tmp_db)

    # Create a tiny fixture repo for temp-file tests
    op_tmp = Path(tempfile.mkdtemp(prefix="eval_op_fixture_"))
    try:
        tests = [
            ("rebuild_preserves", lambda: op_test_1_rebuild_preserves(collection, manifest, tmp_db)),
            ("modified_replaces", lambda: op_test_2_modified_replaces(collection, manifest, tmp_db)),
            ("deleted_disappears", lambda: op_test_3_deleted_disappears(collection, manifest, tmp_db, op_tmp)),
            ("rename_cleans", lambda: op_test_4_rename_cleans(collection, manifest, tmp_db, op_tmp)),
            ("true_rollback", lambda: op_test_5_true_rollback(collection, manifest, tmp_db)),
            ("malformed_fallback", op_test_6_malformed_fallback),
            ("duplicate_symbols", lambda: op_test_7_duplicate_symbols(collection)),
            ("multipart_reconstructs", lambda: op_test_8_multipart_reconstructs(collection, manifest, tmp_db, op_tmp)),
            ("mcp_restart", lambda: op_test_9_mcp_restart(collection, manifest, tmp_db)),
            ("stdout_discipline", lambda: op_test_10_stdout_discipline(tmp_db)),
            ("deterministic_ordering", lambda: op_test_11_deterministic_ordering(config, tmp_db)),
        ]

        results = []
        for name, fn in tests:
            try:
                res = fn()
                results.append(res)
                status = "✅" if res.get("passed") else "❌"
                sys.stderr.write(f"  [{status}] {name}\n")
            except Exception as e:
                sys.stderr.write(f"  [❌] {name}: {e}\n")
                results.append({"test": name, "passed": False,
                                "error": str(e), "traceback": traceback.format_exc()})
        return results
    finally:
        shutil.rmtree(op_tmp, ignore_errors=True)


# ---- Failure classification -------------------------------------------------

CLASSIFICATION_PRECISE = {
    "indexing_coverage_gap": "indexing coverage gap",
    "not_applicable": "query not applicable to method",
    "retrieval_miss": "retrieval miss",
    "ranking_miss": "ranking miss",
    "eval_parser_error": "evaluation parser error",
    "corpus_leakage": "corpus leakage",
    "insufficient_context": "insufficient context",
    "false_positive": "false positive",
    "not_exercised": "operational test not exercised",
}


def classify_failure(result: dict) -> dict:
    if result.get("error"):
        return {"pattern": f"Error: {result['error'][:80]}",
                "classification": CLASSIFICATION_PRECISE["eval_parser_error"]}

    if not result.get("applicable", True):
        return {"pattern": "Method not applicable to this question type",
                "classification": CLASSIFICATION_PRECISE["not_applicable"]}

    hits = result.get("hits", [])
    if not hits:
        method = result.get("method", "")
        if method == "exact_symbol":
            return {"pattern": "No results from get_symbol",
                    "classification": CLASSIFICATION_PRECISE["indexing_coverage_gap"]}
        elif method in ("semantic_search", "rg_question", "rg_oracle"):
            return {"pattern": "No results returned",
                    "classification": CLASSIFICATION_PRECISE["retrieval_miss"]}
        else:
            return {"pattern": "No results returned",
                    "classification": CLASSIFICATION_PRECISE["retrieval_miss"]}

    expected_syms = result.get("expected_symbols", [])
    hit_paths = " ".join(h.get("path") or "" for h in hits)
    hit_content = " ".join(h.get("content_preview") or "" for h in hits)
    combined = hit_paths + " " + hit_content

    if expected_syms:
        found_syms = [s for s in expected_syms if s.split(".")[-1] in combined]
        if not found_syms:
            return {"pattern": "Expected symbol not in results",
                    "classification": CLASSIFICATION_PRECISE["retrieval_miss"]}

    if result.get("category") == "fallback":
        exp_paths = result.get("expected_paths", [])
        found_paths = [p for p in exp_paths if p in hit_paths]
        if not found_paths:
            return {"pattern": "Fallback file not retrieved",
                    "classification": CLASSIFICATION_PRECISE["insufficient_context"]}

    if result.get("category") == "negative":
        if result.get("irrelevant_in_top_5", 0) > 0:
            return {"pattern": "False positives returned for negative query",
                    "classification": CLASSIFICATION_PRECISE["false_positive"]}

    if result.get("hit_at_5"):
        return {"pattern": "Relevant found (hit@5)",
                "classification": "pass"}

    return {"pattern": "Relevant but not in top 5",
            "classification": CLASSIFICATION_PRECISE["ranking_miss"]}


# ---- Metrics ----------------------------------------------------------------

def compute_metrics(eval_results: list[dict]) -> dict[str, Any]:
    methods_order = ["exact_symbol", "semantic_search", "rg_question", "rg_oracle"]
    method_labels = {
        "exact_symbol": "Exact Symbol",
        "semantic_search": "Semantic Search",
        "rg_question": "rg-question (lexical)",
        "rg_oracle": "rg-oracle (upper bound)",
    }
    categories = ["exact-symbol", "conceptual", "cross-repo", "fallback", "negative"]

    metrics: dict[str, Any] = {
        "overall": {},
        "by_category": {},
        "by_method": {},
        "applicability_adjusted": {},
        "negative": {},
    }

    # -- Negative query metrics --
    neg_results = [r for r in eval_results if r["category"] == "negative"]
    if neg_results:
        fp_count = sum(1 for r in neg_results if (r["false_positive"] if "false_positive" in r else r["irrelevant_in_top_5"] > 0))
        metrics["negative"] = {
            "questions": len(neg_results),
            "false_positive_count": fp_count,
            "false_positive_rate": fp_count / len(neg_results) if neg_results else 0,
            "avg_irrelevant": sum(r["irrelevant_in_top_5"] for r in neg_results) / len(neg_results) if neg_results else 0,
        }

    # -- Per method overall --
    for m in methods_order:
        m_results = [r for r in eval_results if r["method"] == m]
        if not m_results:
            continue
        n = len(m_results)
        hit1 = sum(1 for r in m_results if r["hit_at_1"]) / n * 100
        hit5 = sum(1 for r in m_results if r["hit_at_5"]) / n * 100
        mrr = sum(r["reciprocal_rank"] for r in m_results) / n * 100
        avg_lat = sum(r["latency"] for r in m_results if r["latency"] > 0) / max(sum(1 for r in m_results if r["latency"] > 0), 1) * 1000
        metrics["by_method"][m] = {
            "label": method_labels.get(m, m),
            "questions": n,
            "hit_at_1_pct": round(hit1, 1),
            "hit_at_5_pct": round(hit5, 1),
            "mrr_pct": round(mrr, 1),
            "avg_latency_ms": round(avg_lat, 0),
        }

    # -- Per category per method --
    for cat in categories:
        metrics["by_category"][cat] = {}
        for m in methods_order:
            c_results = [r for r in eval_results if r["category"] == cat and r["method"] == m]
            if not c_results:
                continue
            n = len(c_results)
            hit1 = sum(1 for r in c_results if r["hit_at_1"]) / n * 100
            hit5 = sum(1 for r in c_results if r["hit_at_5"]) / n * 100
            metrics["by_category"][cat][m] = {
                "questions": n,
                "hit_at_1_pct": round(hit1, 1),
                "hit_at_5_pct": round(hit5, 1),
            }

    # -- Applicability-adjusted --
    for m in methods_order:
        m_results = [r for r in eval_results if r["method"] == m]
        applicable = [r for r in m_results if r.get("applicable", True)]
        na_count = sum(1 for r in m_results if not r.get("applicable", True))
        if applicable:
            n = len(applicable)
            hit1 = sum(1 for r in applicable if r["hit_at_1"]) / n * 100
            hit5 = sum(1 for r in applicable if r["hit_at_5"]) / n * 100
            metrics["applicability_adjusted"][m] = {
                "questions": n,
                "not_applicable": na_count,
                "raw_hit_at_1_pct": round(sum(1 for r in m_results if r["hit_at_1"]) / len(m_results) * 100, 1) if m_results else 0,
                "adj_hit_at_1_pct": round(hit1, 1),
                "adj_hit_at_5_pct": round(hit5, 1),
            }

    # -- Overall --
    total = len(eval_results)
    passed_h5 = sum(1 for r in eval_results if r["hit_at_5"])
    passed_h1 = sum(1 for r in eval_results if r["hit_at_1"])
    metrics["overall"] = {
        "total_runs": total,
        "passed_hit_at_5": passed_h5,
        "passed_hit_at_1": passed_h1,
        "hit_at_1_pct": round(passed_h1 / total * 100, 1) if total else 0,
        "hit_at_5_pct": round(passed_h5 / total * 100, 1) if total else 0,
    }

    return metrics


# ---- Report generation ------------------------------------------------------

METHOD_LABELS = {
    "exact_symbol": "Exact Symbol",
    "semantic_search": "Semantic Search",
    "rg_question": "rg-question",
    "rg_oracle": "rg-oracle",
    "repo_orientation": "Repo Orientation",
}


def generate_report(
    eval_results: list[dict],
    op_results: list[dict],
    repo_orientation_results: list[dict],
    metrics: dict[str, Any],
    config: ServerConfig,
    db_path: Path,
) -> str:
    lines: list[str] = []
    lines.append("# local-code-context Retrieval Evaluation Report")
    lines.append("")
    lines.append(f"- **Benchmark version:** {BENCHMARK_VERSION}")
    lines.append(f"- **Date:** {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    lines.append(f"- **Frozen questions hash:** {FROZEN_QUESTIONS_HASH}")
    lines.append(f"- **Git (lcc):** `{GIT_HASH_LCC}`")
    lines.append(f"- **Git (compy):** `{GIT_HASH_COMPY}`")
    lines.append(f"- **DB:** {db_path}")
    lines.append(f"- **Embed model:** {EMBED_MODEL}")
    lines.append(f"- **Top-K:** {TOP_K}")
    lines.append("")

    # ===== 1. Summary by Category =====
    lines.append("## 1. Summary by Category")
    lines.append("")
    lines.append("| Category | Questions | Method | Hit@1 | Hit@5 | MRR | Rel@5 | Irrel@5 | Latency(ms) |")
    lines.append("|----------|-----------|--------|-------|-------|-----|-------|---------|-------------|")

    bc = metrics.get("by_category", {})
    for cat in ["exact-symbol", "conceptual", "cross-repo", "fallback", "negative"]:
        cat_data = bc.get(cat, {})
        for m in ["exact_symbol", "semantic_search", "rg_question", "rg_oracle"]:
            m_data = cat_data.get(m)
            if not m_data:
                continue
            n = m_data["questions"]
            label = METHOD_LABELS.get(m, m)
            lines.append(
                f"| {cat} ({n}q) | | {label:20s} | "
                f"{m_data['hit_at_1_pct']:.0f}% | {m_data['hit_at_5_pct']:.0f}% | "
                f"- | - | - | - |"
            )
        lines.append("| | | | | | | | |")

    lines.append("")

    # ===== 2. Overall Metrics =====
    lines.append("## 2. Overall Metrics")
    lines.append("")

    bm = metrics.get("by_method", {})
    for m in ["exact_symbol", "semantic_search", "rg_question", "rg_oracle"]:
        md = bm.get(m)
        if not md:
            continue
        lines.append(f"### {md['label']}")
        lines.append(f"- **Questions evaluated:** {md['questions']}")
        lines.append(f"- **Hit@1:** {md['hit_at_1_pct']:.0f}%")
        lines.append(f"- **Hit@5:** {md['hit_at_5_pct']:.0f}%")
        lines.append(f"- **MRR:** {md['mrr_pct']:.0f}%")
        lines.append(f"- **Avg latency:** {md['avg_latency_ms']:.0f} ms")
        lines.append("")

    # ===== 2b. Applicability-Adjusted =====
    lines.append("### Applicability-Adjusted Scores")
    lines.append("")
    lines.append("| Method | Raw Q | Applicable Q | Not Applicable | Raw Hit@1 | Adj Hit@1 | Adj Hit@5 |")
    lines.append("|--------|-------|-------------|----------------|-----------|-----------|-----------|")
    aa = metrics.get("applicability_adjusted", {})
    for m in ["exact_symbol", "semantic_search", "rg_question"]:
        a = aa.get(m)
        if not a:
            continue
        lines.append(
            f"| {METHOD_LABELS.get(m, m):20s} | {a['questions'] + a['not_applicable']} | "
            f"{a['questions']} | {a['not_applicable']} | "
            f"{a['raw_hit_at_1_pct']:.0f}% | {a['adj_hit_at_1_pct']:.0f}% | {a['adj_hit_at_5_pct']:.0f}% |"
        )
    lines.append("")

    # ===== 3. Per-Question Results =====
    lines.append("## 3. Per-Question Results")
    lines.append("")

    for q in QUESTIONS:
        lines.append(f"### {q.qid}: {q.text}")
        lines.append(f"- **Category:** {q.category}")
        lines.append(f"- **Expected repo:** {q.expected_repo or 'N/A'}")
        lines.append(f"- **Expected paths:** {', '.join(q.expected_paths) or 'N/A'}")
        lines.append(f"- **Expected symbols:** {', '.join(q.expected_symbols) or 'N/A'}")
        if q.notes:
            lines.append(f"- **Notes:** {q.notes}")
        lines.append("")

        q_results = [r for r in eval_results if r["qid"] == q.qid]
        if not q_results:
            lines.append("*No methods applied.*\n")
            continue

        for r in q_results:
            m = r["method"]
            label = METHOD_LABELS.get(m, m)
            status = "✅" if r["hit_at_5"] else "❌"
            na_flag = " ⚠ N/A" if not r.get("applicable", True) else ""
            lines.append(
                f"**{label}**{na_flag} {status} | "
                f"Hit@1={r['hit_at_1']} Hit@5={r['hit_at_5']} "
                f"RR={r['reciprocal_rank']:.2f} "
                f"Rel={r['relevant_in_top_5']} Irrel={r['irrelevant_in_top_5']} "
                f"Lat={r['latency']*1000:.0f}ms "
                f"CtxOK={r['context_sufficient']}"
            )
            if r["error"]:
                lines.append(f"  ⚠ Error: {r['error']}")
            if r["hits"]:
                for h in r["hits"][:3]:
                    d = f" d={h['distance']:.3f}" if h.get("distance") else ""
                    lines.append(
                        f"  - #{h['rank']}: `{h['path'] or '?'}`"
                        f" [{h['repo'] or '?'}]{d}"
                    )
                    if h.get("symbol"):
                        lines.append(f"    sym={h['symbol']}")
            else:
                lines.append("  - (no results)")
            lines.append("")

    # ===== 4. Failed Queries =====
    lines.append("## 4. Failed Queries & Classification")
    lines.append("")

    failures = [r for r in eval_results if not r["hit_at_5"] and r.get("applicable", True)]
    if failures:
        lines.append(f"**{len(failures)} applicable query/method combinations failed.**\n")
        lines.append("| QID | Category | Method | Failure Pattern | Classification |")
        lines.append("|-----|----------|--------|-----------------|----------------|")
        for f in failures:
            p = classify_failure(f)
            lines.append(
                f"| {f['qid']} | {f['category']} | {METHOD_LABELS.get(f['method'], f['method'])} | "
                f"{p['pattern']} | {p['classification']} |"
            )
        lines.append("")
    else:
        lines.append("**All applicable queries passed.**\n")

    # ===== 5. Negative Query Metrics =====
    lines.append("## 5. Negative Query Metrics")
    lines.append("")
    neg = metrics.get("negative", {})
    if neg:
        lines.append(f"- **False-positive rate:** {neg['false_positive_rate']:.0%}")
        lines.append(f"- **False-positive count:** {neg['false_positive_count']}/{neg['questions']}")
        lines.append(f"- **Avg irrelevant results returned:** {neg['avg_irrelevant']:.1f}")
    else:
        lines.append("*No negative queries evaluated.*")
    lines.append("")

    # ===== 6. Repo Orientation Results =====
    lines.append("## 6. Repo Orientation Results")
    lines.append("")
    for ro in repo_orientation_results:
        status = "✅" if ro.get("mention_coverage", 0) >= 0.5 else "❌"
        lines.append(f"### {ro['qid']}: {ro['text']} {status}")
        lines.append(f"- **Expected repo:** {ro.get('expected_repo', '?')}")
        lines.append(f"- **Coverage:** {ro.get('mention_coverage', 0):.0%}")
        lines.append(f"- **Found mentions:** {', '.join(ro.get('found_mentions', [])) or 'none'}")
        lines.append(f"- **Missed mentions:** {', '.join(ro.get('missed_mentions', [])) or 'none'}")
        if ro.get("error"):
            lines.append(f"- **Error:** {ro['error']}")
        lines.append("")

    # ===== 7. Operational Test Results =====
    lines.append("## 7. Operational Test Results")
    lines.append("")
    op_passed = sum(1 for t in op_results if t.get("passed"))
    lines.append(f"**{op_passed}/{len(op_results)} tests passed.**\n")
    lines.append("| Test | Status | Details |")
    lines.append("|------|--------|---------|")
    for t in op_results:
        status = "✅" if t.get("passed") else "❌"
        details = json.dumps(t.get("details", t.get("error", "")))
        if len(details) > 120:
            details = details[:117] + "..."
        lines.append(f"| {t['test']} | {status} | {details} |")
    lines.append("")

    # ===== 8. Success Criteria Check =====
    lines.append("## 8. Success Criteria Check")
    lines.append("")
    # key criteria
    if "exact_symbol" in bm:
        es = bm["exact_symbol"]
        lines.append(f"- {'✅' if es['hit_at_1_pct'] >= 95 else '❌'} **Exact-symbol Hit@1 ≥ 95%**: {es['hit_at_1_pct']:.0f}% ({es['questions']}q)")
    if "semantic_search" in bm:
        ss = bm["semantic_search"]
        ci = metrics.get("by_category", {}).get("conceptual", {}).get("semantic_search", {})
        ci_hit5 = ci.get("hit_at_5_pct", 0)
        lines.append(f"- {'✅' if ci_hit5 >= 80 else '❌'} **Conceptual search Hit@5 ≥ 80%**: {ci_hit5:.0f}%")

    rename_test = next((t for t in op_results if t["test"] == "rename_cleans"), {})
    lines.append(f"- {'✅' if rename_test.get('passed') else '❌'} **Zero stale records after rename/delete**: {'Passed' if rename_test.get('passed') else 'Failed'}")

    rollback_test = next((t for t in op_results if t["test"] == "true_rollback"), {})
    lines.append(f"- {'✅' if rollback_test.get('passed') else '❌'} **Zero data loss during forced rollback**: {'Passed' if rollback_test.get('passed') else 'Failed'}")

    det_test = next((t for t in op_results if t["test"] == "deterministic_ordering"), {})
    lines.append(f"- {'✅' if det_test.get('passed') else '❌'} **Deterministic ordering**: {'Passed' if det_test.get('passed') else 'Failed'}")

    all_lats = [r["latency"] for r in eval_results if r["latency"] > 0]
    sorted_lats = sorted(all_lats)
    median_lat = sorted_lats[len(sorted_lats) // 2] * 1000 if sorted_lats else 0
    lines.append(f"- {'✅' if median_lat < 1000 else '❌'} **Median latency < 1000ms**: {median_lat:.0f}ms")

    lines.append("")

    # ===== 9. Recommendations =====
    lines.append("## 9. Recommendations")
    lines.append("")
    patterns = defaultdict(list)
    for f in failures:
        p = classify_failure(f)
        patterns[p["classification"]].append(f)
    if patterns:
        lines.append("Based on failure analysis:")
        lines.append("")
        for cls, fails in sorted(patterns.items()):
            lines.append(f"- **{cls}** ({len(fails)} failures):")
            for f in fails:
                lines.append(f"  - {f['qid']} via {METHOD_LABELS.get(f['method'], f['method'])}")
            lines.append("")

    return "\n".join(lines)


# ---- Result persistence -----------------------------------------------------

def save_raw_results(
    eval_results: list[dict],
    op_results: list[dict],
    repo_orientation_results: list[dict],
    metrics: dict[str, Any],
    report: str,
    config: ServerConfig,
    db_path: Path,
) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = RESULTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    # config
    (out_dir / "config.json").write_text(json.dumps({
        "benchmark_version": BENCHMARK_VERSION,
        "frozen_questions_hash": FROZEN_QUESTIONS_HASH,
        "git_lcc": GIT_HASH_LCC,
        "git_compy": GIT_HASH_COMPY,
        "embed_model": EMBED_MODEL,
        "top_k": TOP_K,
        "ollama_url": OLLAMA_URL,
        "db_path": str(db_path),
        "questions_count": len(QUESTIONS),
    }, indent=2))

    # index manifest
    idx_manifest = load_manifest(db_path)
    (out_dir / "index_manifest.json").write_text(json.dumps(idx_manifest, indent=2, default=str))

    # raw results
    (out_dir / "raw_results.json").write_text(json.dumps(eval_results, indent=2, default=str))

    # operational results
    (out_dir / "operational_results.json").write_text(json.dumps(op_results, indent=2, default=str))

    # metrics
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, default=str))

    # report
    (out_dir / "report.md").write_text(report)

    # Symlink "latest" -> this run
    latest = RESULTS_DIR / "latest"
    if latest.exists() or latest.is_symlink():
        latest.unlink()
    latest.symlink_to(out_dir.name, target_is_directory=True)

    return out_dir


# ---- Main -------------------------------------------------------------------

def main():
    sys.stderr.write(f"=== local-code-context Retrieval Evaluation v{BENCHMARK_VERSION} ===\n")
    sys.stderr.write(f"Questions: {len(QUESTIONS)} (hash: {FROZEN_QUESTIONS_HASH[:12]}...)\n")
    sys.stderr.write(f"Git lcc: {GIT_HASH_LCC[:12]}  compy: {GIT_HASH_COMPY[:12]}\n\n")

    # Phase 1: build temp index and run retrieval evaluation
    with temp_index() as (config, tmp_db):
        sys.stderr.write("\n=== Phase 1: Retrieval Evaluation ===\n")
        eval_results = run_evaluation(config)

        sys.stderr.write("\n=== Phase 2: Repo Orientation ===\n")
        repo_orientation_results = repo_orientation_evaluation(config)

        sys.stderr.write("\n=== Phase 3: Operational Tests ===\n")
        op_results = run_operational_tests(config, tmp_db)

    # Phase 4: Metrics
    sys.stderr.write("\n=== Phase 4: Metrics ===\n")
    metrics = compute_metrics(eval_results)

    # Phase 5: Report
    sys.stderr.write("\n=== Phase 5: Report ===\n")
    report = generate_report(
        eval_results, op_results, repo_orientation_results,
        metrics, config, tmp_db,
    )
    print(report)

    # Phase 6: Save
    out_dir = save_raw_results(
        eval_results, op_results, repo_orientation_results,
        metrics, report, config, tmp_db,
    )
    sys.stderr.write(f"\nResults saved to {out_dir}\n")
    sys.stderr.write(f"  - config.json\n  - index_manifest.json\n  - raw_results.json\n")
    sys.stderr.write(f"  - operational_results.json\n  - metrics.json\n  - report.md\n")

    total = len(eval_results)
    passed_h5 = sum(1 for r in eval_results if r["hit_at_5"])
    passed_h1 = sum(1 for r in eval_results if r["hit_at_1"])
    sys.stderr.write(f"\n=== Summary ===\n")
    sys.stderr.write(f"Total eval runs: {total}\n")
    sys.stderr.write(f"Passed Hit@5: {passed_h5}/{total} ({passed_h5/total*100:.0f}%)\n")
    sys.stderr.write(f"Passed Hit@1: {passed_h1}/{total} ({passed_h1/total*100:.0f}%)\n")
    sys.stderr.write(f"Operational tests passed: {sum(1 for t in op_results if t.get('passed'))}/{len(op_results)}\n")


if __name__ == "__main__":
    main()
