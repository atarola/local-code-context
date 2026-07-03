from __future__ import annotations

from typing import Any

from local_code_context.retrieval.models import HybridCandidate, HybridResult

_RANKING_WEIGHTS = {
    "semantic": 1.0,
    "lexical": 0.6,
    "exact_symbol": 0.4,
    "source_agreement_bonus": 0.3,
    "chunk_type_bonus_symbol": 0.15,
    "chunk_type_bonus_symbol_part": 0.1,
    "chunk_type_bonus_text": 0.0,
    "chunk_type_bonus_file_map": -0.05,
    "path_role_bonus_implementation": 0.1,
    "path_role_bonus_test": 0.0,
    "path_role_bonus_documentation": -0.05,
    "path_role_bonus_configuration": -0.05,
    "path_role_bonus_evaluation": -0.2,
    "path_role_bonus_generated": -0.2,
    "path_role_bonus_unknown": 0.0,
}


def get_weights() -> dict[str, float]:
    return dict(_RANKING_WEIGHTS)


def set_weights(overrides: dict[str, float]) -> None:
    _RANKING_WEIGHTS.update(overrides)


def normalize_semantic_distance(distance: float | None) -> float:
    if distance is None:
        return 0.0
    return max(0.0, 1.0 - distance)


def normalize_lexical(has_match: bool, n_terms_matched: int, n_terms_total: int) -> float:
    if not has_match or n_terms_total == 0:
        return 0.0
    return min(1.0, n_terms_matched / n_terms_total)


def normalize_exact_symbol(is_match: bool) -> float:
    return 1.0 if is_match else 0.0


def classify_path_role(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    name = parts[-1] if parts else path

    top = parts[0] if parts else ""

    if top in {"evaluation", "results"} or path.startswith("evaluation/"):
        return "evaluation"

    if name in {"Cargo.lock", "uv.lock", "package-lock.json", "yarn.lock", "poetry.lock"}:
        return "generated"
    if name.endswith(".pyc") or name.endswith(".pyo"):
        return "generated"
    if path.endswith(".kicad_pro") or path.endswith(".kicad_prl") or path.endswith(".kicad_sch") or path.endswith(".kicad_pcb"):
        return "generated"

    if top in {"test", "tests", "__tests__"} or name.startswith("test_") or name.endswith(
        (".test.py", ".spec.py", ".test.ts", ".spec.ts", ".test.js", ".spec.js",
         ".test.rs", ".spec.rs")
    ):
        return "test"

    if name.lower() in {"readme.md", "readme", "license", "licenses"} or path.endswith(".md"):
        return "documentation"

    if name in {
        "pyproject.toml", "Cargo.toml", "go.mod", "package.json",
        "Makefile", "justfile", "docker-compose.yml", "compose.yml",
        "flake.nix", "shell.nix",
    } or name.endswith((".cfg", ".conf", ".ini", ".toml", ".yaml", ".yml", ".json")):
        return "configuration"

    if top in {"src", "lib", "app", "cmd", "internal", "pkg"} or path.endswith(
        (".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".c", ".h", ".cpp", ".hpp", ".nix")
    ):
        return "implementation"

    return "unknown"


def compute_final_score(candidate: HybridCandidate) -> float:
    w = _RANKING_WEIGHTS

    score = (
        w["semantic"] * candidate.semantic_score
        + w["lexical"] * candidate.lexical_score
        + w["exact_symbol"] * candidate.exact_symbol_score
    )

    n_sources = len(set(candidate.match_sources))
    if n_sources >= 2:
        score += w["source_agreement_bonus"] * (n_sources - 1)

    ct = candidate.chunk_type or ""
    if ct == "symbol":
        score += w["chunk_type_bonus_symbol"]
    elif ct == "symbol_part":
        score += w["chunk_type_bonus_symbol_part"]
    elif ct == "text":
        score += w["chunk_type_bonus_text"]
    elif ct == "file_map":
        score += w["chunk_type_bonus_file_map"]

    role = classify_path_role(candidate.path)
    candidate.path_role = role
    role_key = f"path_role_bonus_{role}"
    score += w.get(role_key, 0.0)

    return score


def _sort_key(result: HybridResult) -> tuple:
    return (
        -result.final_score,
        -result.exact_symbol_score,
        -result.semantic_score,
        -result.lexical_score,
        result.repo or "",
        result.path or "",
        result.start_line or 0,
        result.part_index or 0,
        result.id or "",
    )


def rerank(candidates: list[HybridCandidate], limit: int) -> list[HybridResult]:
    for c in candidates:
        c.path_role = classify_path_role(c.path)

    scored = [(compute_final_score(c), c) for c in candidates]
    scored.sort(key=lambda x: -x[0])

    results: list[HybridResult] = []
    seen_ids: set[str] = set()
    seen_composite: set[tuple[Any, ...]] = set()

    for final_score, c in scored:
        rid = c.record_id
        if rid and rid in seen_ids:
            continue
        if rid:
            seen_ids.add(rid)
        else:
            comp = (
                c.repo, c.path, c.chunk_type or "", c.symbol or "",
                c.symbol_kind or "", c.parent_symbol or "",
                c.start_line, c.end_line, c.part_index,
            )
            if comp in seen_composite:
                continue
            seen_composite.add(comp)

        results.append(HybridResult(
            id=rid or "",
            repo=c.repo,
            path=c.path,
            language=c.language,
            chunk_type=c.chunk_type,
            symbol=c.symbol,
            symbol_kind=c.symbol_kind,
            parent_symbol=c.parent_symbol,
            start_line=c.start_line,
            end_line=c.end_line,
            part_index=c.part_index,
            match_sources=sorted(set(c.match_sources)),
            semantic_score=c.semantic_score,
            lexical_score=c.lexical_score,
            exact_symbol_score=c.exact_symbol_score,
            final_score=final_score,
            path_role=c.path_role,
            document=c.document,
        ))

    results.sort(key=_sort_key)
    return results[:limit]
