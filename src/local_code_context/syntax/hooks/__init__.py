from __future__ import annotations

from local_code_context.syntax.capture_models import QueryLanguageHooks
from local_code_context.syntax.hooks.python import PYTHON_HOOKS
from local_code_context.syntax.hooks.rust import RUST_HOOKS


QUERY_LANGUAGE_HOOKS: dict[str, QueryLanguageHooks] = {
    "python": PYTHON_HOOKS,
    "rust": RUST_HOOKS,
}

