from __future__ import annotations

import sys
import types
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from local_code_context.retrieval.hybrid import (
    _needs_lexical_boost,
    _is_likely_negative,
    NO_RESULT_RAW_THRESHOLD,
)
from local_code_context.retrieval.models import (
    HybridCandidate,
    HybridResult,
    candidate_identity,
    composite_identity,
)
from local_code_context.retrieval.query_intent import classify_query, extract_identifiers
from local_code_context.retrieval.lexical import _extract_terms, _run_rg
from local_code_context.retrieval.ranking import (
    classify_path_role,
    compute_final_score,
    get_weights,
    normalize_exact_symbol,
    normalize_lexical,
    normalize_semantic_distance,
    rerank,
    set_weights,
)


class FakeConfig:
    def __init__(self):
        self.db = Path("/tmp/test_db")
        self.collection = "code_chunks"
        self.top_k = 5
        self.embed_model = "nomic-embed-text"
        self.model = "test"
        self.ollama_url = "http://127.0.0.1:11434"
        self.repo = None


class TestModels(unittest.TestCase):
    def test_hybrid_candidate_defaults(self) -> None:
        c = HybridCandidate(
            record_id="id1", repo="test", path="a.py", language="python",
            chunk_type="symbol", symbol="Foo", symbol_kind="class",
            parent_symbol=None, start_line=1, end_line=10, part_index=None,
            document="class Foo: pass",
        )
        self.assertEqual(c.semantic_score, 0.0)
        self.assertEqual(c.lexical_score, 0.0)
        self.assertEqual(c.exact_symbol_score, 0.0)
        self.assertEqual(c.match_sources, [])
        self.assertEqual(c.path_role, "unknown")

    def test_hybrid_result_fields(self) -> None:
        r = HybridResult(
            id="r1", repo="test", path="b.py",
            final_score=0.85, semantic_score=0.7, lexical_score=0.5,
            exact_symbol_score=0.0, match_sources=["semantic"],
            path_role="implementation", document="code",
        )
        self.assertEqual(r.final_score, 0.85)
        self.assertEqual(r.path_role, "implementation")

    def test_composite_identity_from_meta(self) -> None:
        meta = {
            "repo": "r", "path": "p", "chunk_type": "symbol",
            "symbol": "X", "symbol_kind": "function",
            "parent_symbol": None, "start_line": 1, "end_line": 5,
            "part_index": None,
        }
        ident = composite_identity(meta)
        self.assertEqual(ident[0], "r")
        self.assertEqual(ident[1], "p")
        self.assertIsNone(ident[8])

    def test_candidate_identity(self) -> None:
        c = HybridCandidate(
            record_id=None, repo="r", path="p", language="py",
            chunk_type="symbol", symbol="X", symbol_kind="function",
            parent_symbol=None, start_line=1, end_line=5, part_index=None,
            document="x",
        )
        ident = candidate_identity(c)
        self.assertEqual(ident[0], "r")
        self.assertEqual(ident[2], "symbol")


class TestQueryIntent(unittest.TestCase):
    def test_exact_symbol_pattern(self) -> None:
        self.assertEqual(classify_query("find the function search_chunks"), "exact_symbol")
        self.assertEqual(classify_query("Where is compute_foo defined?"), "exact_symbol")
        self.assertEqual(classify_query("Show me the class MainProcessor"), "exact_symbol")
        self.assertEqual(classify_query("Where can I find process_data?"), "exact_symbol")

    def test_implementation_pattern(self) -> None:
        self.assertEqual(classify_query("how does the indexer work?"), "implementation")
        self.assertEqual(classify_query("How is ACIA serial tested?"), "implementation")
        self.assertEqual(classify_query("explain the process of connecting"), "implementation")
        self.assertEqual(classify_query("How are records restored?"), "implementation")

    def test_usage_pattern(self) -> None:
        self.assertEqual(classify_query("Where is search_code used?"), "usage")
        self.assertEqual(classify_query("What calls run_index?"), "usage")
        self.assertEqual(classify_query("Find usages of resolve_path"), "usage")

    def test_orientation_pattern(self) -> None:
        self.assertEqual(classify_query("what are the major subsystems?"), "orientation")
        self.assertEqual(classify_query("What is the architecture?"), "orientation")
        self.assertEqual(classify_query("How is the project structured?"), "orientation")

    def test_fallback_pattern(self) -> None:
        self.assertEqual(classify_query("What is the pin assignment?"), "fallback")
        self.assertEqual(classify_query("Show the pin layout for the 6502"), "fallback")
        self.assertEqual(classify_query("What constants define the vector addresses?"), "fallback")

    def test_general_none_match(self) -> None:
        self.assertEqual(classify_query("write a python script"), "general")
        self.assertEqual(classify_query(""), "general")

    def test_extract_identifiers(self) -> None:
        idents = extract_identifiers("find the function search_chunks")
        self.assertIn("search_chunks", idents)
        self.assertNotIn("the", idents)
        self.assertNotIn("function", idents)

    def test_extract_identifiers_with_quotes(self) -> None:
        idents = extract_identifiers("show me `resolve_path` / `validate`")
        self.assertIn("resolve_path", idents)

    def test_extract_identifiers_empty(self) -> None:
        self.assertEqual(extract_identifiers("what is the process"), [])


class TestLexical(unittest.TestCase):
    def test_extract_terms_removes_stopwords(self) -> None:
        terms = _extract_terms("how does the serial ACIA function")
        self.assertIn("serial", terms)
        self.assertIn("acia", terms)
        self.assertIn("function", terms)
        self.assertNotIn("how", terms)
        self.assertNotIn("the", terms)

    def test_extract_terms_min_length(self) -> None:
        terms = _extract_terms("a is in on at")
        self.assertEqual(terms, [])

    def test_extract_terms_empty(self) -> None:
        self.assertEqual(_extract_terms(""), [])

    def test_extract_terms_case_insensitive(self) -> None:
        terms = _extract_terms("Find Search Chunks")
        self.assertIn("find", terms)
        self.assertIn("search", terms)
        self.assertIn("chunks", terms)

    def test_run_rg_no_terms_returns_empty(self) -> None:
        result = _run_rg("", [], None, None, 10)
        self.assertEqual(result, [])

    def test_run_rg_unsupported_repo_path(self) -> None:
        result = _run_rg(
            "test_query", [], "non_existent_repo", None, 10,
        )
        self.assertEqual(result, [])


class TestRanking(unittest.TestCase):
    def setUp(self):
        self._weights = get_weights()

    def tearDown(self):
        set_weights(self._weights)

    def test_normalize_semantic_distance(self) -> None:
        self.assertAlmostEqual(normalize_semantic_distance(0.0), 1.0)
        self.assertAlmostEqual(normalize_semantic_distance(0.5), 0.5)
        self.assertAlmostEqual(normalize_semantic_distance(1.0), 0.0)
        self.assertAlmostEqual(normalize_semantic_distance(2.0), 0.0)
        self.assertAlmostEqual(normalize_semantic_distance(-0.5), 1.5)
        self.assertAlmostEqual(normalize_semantic_distance(None), 0.0)

    def test_normalize_lexical(self) -> None:
        self.assertAlmostEqual(normalize_lexical(True, 2, 4), 0.5)
        self.assertAlmostEqual(normalize_lexical(True, 0, 4), 0.0)
        self.assertAlmostEqual(normalize_lexical(True, 4, 4), 1.0)
        self.assertAlmostEqual(normalize_lexical(False, 0, 4), 0.0)
        self.assertAlmostEqual(normalize_lexical(True, 3, 0), 0.0)

    def test_normalize_exact_symbol(self) -> None:
        self.assertEqual(normalize_exact_symbol(True), 1.0)
        self.assertEqual(normalize_exact_symbol(False), 0.0)

    def test_classify_path_role_evaluation(self) -> None:
        self.assertEqual(classify_path_role("evaluation/benchmark.py"), "evaluation")
        self.assertEqual(classify_path_role("results/report.md"), "evaluation")
        self.assertEqual(classify_path_role("foo/evaluation/bar.py"), "implementation")

    def test_classify_path_role_test(self) -> None:
        self.assertEqual(classify_path_role("tests/test_foo.py"), "test")
        self.assertEqual(classify_path_role("test_foo.py"), "test")

    def test_classify_path_role_implementation(self) -> None:
        self.assertEqual(classify_path_role("src/main.py"), "implementation")
        self.assertEqual(classify_path_role("lib/core.rs"), "implementation")

    def test_classify_path_role_configuration(self) -> None:
        self.assertEqual(classify_path_role("pyproject.toml"), "configuration")
        self.assertEqual(classify_path_role("config/settings.json"), "configuration")

    def test_classify_path_role_documentation(self) -> None:
        self.assertEqual(classify_path_role("README.md"), "documentation")
        self.assertEqual(classify_path_role("docs/guide.md"), "documentation")

    def test_classify_path_role_generated(self) -> None:
        self.assertEqual(classify_path_role("Cargo.lock"), "generated")
        self.assertEqual(classify_path_role("dist/output.pyc"), "generated")

    def test_classify_path_role_unknown(self) -> None:
        self.assertEqual(classify_path_role("random_file.txt"), "unknown")

    def test_compute_final_score_semantic_only(self) -> None:
        c = HybridCandidate(
            record_id="id", repo="r", path="src/main.py", language="py",
            chunk_type="text", symbol=None, symbol_kind=None,
            parent_symbol=None, start_line=1, end_line=5, part_index=None,
            document="code", semantic_score=0.8,
            match_sources=["semantic"],
        )
        score = compute_final_score(c)
        self.assertGreater(score, 0.0)
        # semantic weight=1.0, implementation path-role bonus=0.1
        self.assertAlmostEqual(score, 0.9)

    def test_compute_final_score_multi_source_bonus(self) -> None:
        c = HybridCandidate(
            record_id="id", repo="r", path="src/main.py", language="py",
            chunk_type="symbol", symbol="Foo", symbol_kind="class",
            parent_symbol=None, start_line=1, end_line=5, part_index=None,
            document="code", semantic_score=0.7, lexical_score=0.6,
            match_sources=["semantic", "lexical"],
        )
        score = compute_final_score(c)
        # semantic=1.0*0.7 + lexical=0.6*0.6 + source_bonus=0.3 + symbol_bonus=0.15 + impl_bonus=0.1
        expected = 0.7 + 0.36 + 0.3 + 0.15 + 0.1
        self.assertAlmostEqual(score, expected)

    def test_rerank_returns_limited_results(self) -> None:
        candidates = [
            HybridCandidate(
                record_id=f"id{i}", repo="r", path=f"src/{i}.py", language="py",
                chunk_type="text", symbol=None, symbol_kind=None,
                parent_symbol=None, start_line=1, end_line=5,
                part_index=None, document=f"code{i}",
                semantic_score=1.0 - i * 0.1,
                match_sources=["semantic"],
            )
            for i in range(10)
        ]
        results = rerank(candidates, 3)
        self.assertEqual(len(results), 3)
        self.assertGreater(results[0].final_score, results[1].final_score)

    def test_rerank_dedup_by_id(self) -> None:
        c = HybridCandidate(
            record_id="dup", repo="r", path="src/a.py", language="py",
            chunk_type="text", symbol=None, symbol_kind=None,
            parent_symbol=None, start_line=1, end_line=5, part_index=None,
            document="code", semantic_score=0.9, match_sources=["semantic"],
        )
        results = rerank([c, c], 5)
        self.assertEqual(len(results), 1)

    def test_sort_key_deterministic(self) -> None:
        c1 = HybridCandidate(
            record_id="a", repo="r", path="src/foo.py", language="py",
            chunk_type="symbol", symbol="Foo", symbol_kind="class",
            parent_symbol=None, start_line=1, end_line=5, part_index=None,
            document="code", semantic_score=0.8, match_sources=["semantic"],
        )
        c2 = HybridCandidate(
            record_id="b", repo="r", path="src/bar.py", language="py",
            chunk_type="symbol", symbol="Bar", symbol_kind="function",
            parent_symbol=None, start_line=1, end_line=5, part_index=None,
            document="code", semantic_score=0.8, match_sources=["semantic"],
        )
        r1 = rerank([c1, c2], 5)
        r2 = rerank([c1, c2], 5)
        self.assertEqual([r.id for r in r1], [r.id for r in r2])

    def test_path_role_assigned_during_rerank(self) -> None:
        c = HybridCandidate(
            record_id="id", repo="r", path="tests/test_x.py", language="py",
            chunk_type="text", symbol=None, symbol_kind=None,
            parent_symbol=None, start_line=1, end_line=5, part_index=None,
            document="code", semantic_score=0.5, match_sources=["semantic"],
        )
        results = rerank([c], 1)
        self.assertEqual(results[0].path_role, "test")

    def test_no_valid_candidates(self) -> None:
        results = rerank([], 5)
        self.assertEqual(results, [])



class TestConfidenceBoost(unittest.TestCase):
    def _make_candidate(self, semantic=0.0, symbol=0.0, **kw):
        return HybridCandidate(
            record_id=kw.get("record_id", "id"),
            repo=kw.get("repo", "r"),
            path=kw.get("path", "src/a.py"),
            language=kw.get("language", "py"),
            chunk_type=kw.get("chunk_type", "text"),
            symbol=kw.get("symbol"),
            symbol_kind=kw.get("symbol_kind"),
            parent_symbol=kw.get("parent_symbol"),
            start_line=kw.get("start_line", 1),
            end_line=kw.get("end_line", 5),
            part_index=kw.get("part_index"),
            document=kw.get("document", "code"),
            semantic_score=semantic,
            exact_symbol_score=symbol,
        )

    def test_boost_needed_when_empty(self) -> None:
        self.assertTrue(_needs_lexical_boost([], []))

    def test_boost_needed_when_all_low(self) -> None:
        sem = [self._make_candidate(semantic=0.01)]
        self.assertTrue(_needs_lexical_boost(sem, []))

    def test_boost_not_needed_high_semantic(self) -> None:
        sem = [self._make_candidate(semantic=0.5)]
        self.assertFalse(_needs_lexical_boost(sem, []))

    def test_boost_not_needed_high_symbol(self) -> None:
        sym = [self._make_candidate(semantic=0.0, symbol=1.0)]
        self.assertFalse(_needs_lexical_boost([], sym))

    def test_boost_edge_at_threshold(self) -> None:
        sem = [self._make_candidate(semantic=0.02)]
        self.assertFalse(_needs_lexical_boost(sem, []), "0.02 >= 0.02 → no boost")

    def test_boost_below_threshold(self) -> None:
        sem = [self._make_candidate(semantic=0.01)]
        self.assertTrue(_needs_lexical_boost(sem, []), "0.01 < 0.02 → boost")


class TestNoResultThreshold(unittest.TestCase):
    def _make_candidate(self, semantic=0.0, symbol=0.0):
        return HybridCandidate(
            record_id="id", repo="r", path="src/a.py", language="py",
            chunk_type="text", symbol=None, symbol_kind=None,
            parent_symbol=None, start_line=1, end_line=5, part_index=None,
            document="code", semantic_score=semantic,
            exact_symbol_score=symbol,
        )

    def test_empty_both_lists_returns_true(self) -> None:
        self.assertTrue(_is_likely_negative([], []))

    def test_negative_low_semantic_no_symbol(self) -> None:
        sem = [self._make_candidate(semantic=0.05)]
        self.assertTrue(_is_likely_negative(sem, []))

    def test_legitimate_semantic_above_threshold(self) -> None:
        sem = [self._make_candidate(semantic=NO_RESULT_RAW_THRESHOLD + 0.01)]
        self.assertFalse(_is_likely_negative(sem, []))

    def test_symbol_match_above_threshold(self) -> None:
        sym = [self._make_candidate(semantic=0.0, symbol=1.0)]
        self.assertFalse(_is_likely_negative([], sym))

    def test_combined_sem_and_symbol_at_threshold(self) -> None:
        # 0.10 semantic + 0.4 * 0.15 symbol = 0.10 + 0.06 = 0.16 > 0.15
        sem = [self._make_candidate(semantic=0.10)]
        sym = [self._make_candidate(semantic=0.0, symbol=0.15)]
        self.assertFalse(_is_likely_negative(sem, sym))

    def test_at_exact_threshold_kept(self) -> None:
        # 0.15 + 0.4 * 0.0 = 0.15 → not < 0.15 → not negative
        sem = [self._make_candidate(semantic=NO_RESULT_RAW_THRESHOLD)]
        self.assertFalse(_is_likely_negative(sem, []))


if __name__ == "__main__":
    unittest.main()
