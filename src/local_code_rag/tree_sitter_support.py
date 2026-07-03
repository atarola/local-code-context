from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - optional dependency
    from tree_sitter import Language, Parser
except Exception:  # pragma: no cover - optional dependency
    Language = None  # type: ignore[assignment]
    Parser = None  # type: ignore[assignment]

try:  # pragma: no cover - optional dependency
    import tree_sitter_python
except Exception:  # pragma: no cover - optional dependency
    tree_sitter_python = None  # type: ignore[assignment]


def _set_parser_language(parser: Any, language: Any) -> bool:
    if hasattr(parser, "language"):
        try:
            parser.language = language
            return True
        except Exception:
            pass
    if hasattr(parser, "set_language"):
        try:
            parser.set_language(language)
            return True
        except Exception:
            pass
    return False


def _build_python_language() -> Any | None:
    if tree_sitter_python is None:
        return None

    language_factory = getattr(tree_sitter_python, "language", None)
    if language_factory is None:
        return None

    try:
        language = language_factory()
    except Exception as exc:
        print(f"failed to load tree-sitter Python grammar: {exc}", file=sys.stderr)
        return None

    if Language is not None and not isinstance(language, Language):
        try:
            language = Language(language)
        except Exception:
            pass
    return language


def _build_python_parser() -> Any | None:
    if Parser is None or tree_sitter_python is None:
        return None

    parser = Parser()
    language = _build_python_language()
    if language is None:
        return None

    if not _set_parser_language(parser, language):
        return None
    return parser


@dataclass
class ParserRegistry:
    _parsers: dict[str, Any | None] = field(default_factory=dict)
    _languages: dict[str, Any | None] = field(default_factory=dict)

    def get(self, language: str) -> Any | None:
        key = language.lower()
        if key in self._parsers:
            return self._parsers[key]

        parser: Any | None
        parser_language: Any | None = None
        if key == "python":
            try:
                built = _build_python_parser()
            except Exception as exc:  # pragma: no cover - defensive
                print(
                    f"failed to load tree-sitter parser for python: {exc}",
                    file=sys.stderr,
                )
                parser = None
            else:
                if isinstance(built, tuple) and len(built) == 2:
                    parser, parser_language = built
                else:
                    parser = built
                    if parser is not None:
                        parser_language = _build_python_language()
        else:
            parser = None

        if parser is None:
            self._parsers[key] = None
            self._languages[key] = None
            return None

        self._parsers[key] = parser
        self._languages[key] = parser_language
        return parser

    def get_language(self, language: str) -> Any | None:
        key = language.lower()
        if key in self._languages:
            return self._languages[key]
        self.get(language)
        return self._languages.get(key)


_DEFAULT_PARSER_REGISTRY = ParserRegistry()


def get_parser_registry() -> ParserRegistry:
    return _DEFAULT_PARSER_REGISTRY
