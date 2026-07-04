from __future__ import annotations

from pathlib import Path


LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".v": "verilog",
    ".vh": "verilog",
    ".sv": "verilog",
    ".svh": "verilog",
    ".s": "assembly",
    ".inc": "assembly",
}


def _looks_like_rust_manifest(repository_hints: set[str] | None) -> bool:
    if not repository_hints:
        return False
    return any(hint.lower() == "cargo.toml" for hint in repository_hints)


def _looks_like_python_shebang(source: bytes) -> bool:
    first_line = source.splitlines()[:1]
    if not first_line:
        return False

    line = first_line[0].decode("utf-8", errors="ignore").strip()
    if not line.startswith("#!"):
        return False

    shebang = line[2:].lower()
    return any(
        token in shebang
        for token in (
            "python",
            "python3",
            "/usr/bin/env python",
            "/usr/bin/env python3",
            "/usr/bin/python",
            "/usr/bin/python3",
        )
    )


def detect_language(
    path: Path,
    source: bytes,
    repository_hints: set[str] | None = None,
) -> str | None:
    suffix = path.suffix.lower()
    if suffix in LANGUAGE_BY_SUFFIX:
        return LANGUAGE_BY_SUFFIX[suffix]

    if not suffix and _looks_like_python_shebang(source):
        return "python"

    if not suffix and _looks_like_rust_manifest(repository_hints):
        return "rust"

    return None
