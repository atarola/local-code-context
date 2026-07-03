from __future__ import annotations

import sys
from functools import lru_cache
from importlib.resources import files


QUERY_RESOURCES = {
    "python": "python-tags.scm",
    "rust": "rust-tags.scm",
}


@lru_cache(maxsize=None)
def load_tags_query(language: str) -> str | None:
    resource_name = QUERY_RESOURCES.get(language.lower())
    if resource_name is None:
        return None

    try:
        return (
            files("local_code_rag.syntax.queries")
            .joinpath(resource_name)
            .read_text(encoding="utf-8")
        )
    except OSError as exc:
        print(
            f"failed to read {language} tags query: {exc}",
            file=sys.stderr,
        )
        return None
