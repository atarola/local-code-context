# Pivot: from chunk embedding to static-analysis cross-reference server

## Motivation

The current index chunks files, embeds them with `nomic-embed-text`, and stores
them in ChromaDB for vector similarity search. This is lossy, noisy, and doesn't
answer the questions a coding model actually needs:

- "Who calls this function?" (grep finds mentions, not callers)
- "What module defines this type?" (chunks may not contain the definition)
- "What imports this crate across the whole repo?" (cross-cutting)

A static-analysis layer — precomputed symbols, imports, call sites, and
cross-file references — produces deterministic, tiny results that slot directly
into model context. No embedding, no vector search, no noise.

## What stays

Everything from the original project: ChromaDB, Ollama embeddings, chunk
pipeline, tree-sitter parsers, text fallback, repo context, watcher.

## What's added

### SQLite cross-reference database (`storage/`)

Parallel to ChromaDB. Lives at `{db}/xref.sqlite`, computed from the existing
`--db` CLI arg. No new flags.

Tables:
- `symbols` — name, kind, language, path, repo, start_line, end_line, parent, exported, signature
- `imports` — source_module, imported_name, path, repo, start_line
- `call_sites` — caller_name, callee_name, path, repo, start_line (schema only, not populated yet)
- `resolved_imports` — maps `import_id` → `symbol_id` via post-indexing resolver pass
- `file_vibe` — per-file one-line summary extracted from first 5 symbol signatures
- `repo_meta` — name, root_path, last_indexed

Writer hooks into `indexing/indexer.py:index_file()` and is called
unconditionally during indexing. Skips files with no symbols.

### Cross-reference resolver (`storage/resolver.py`)

Post-indexing pass (`resolve_imports_for_repo`) that walks the `imports` table
and matches each `imported_name` against `symbols.name` within the same repo.
Also tries extracting the last `::` or `.`-separated component of import paths
(e.g. `crate::display::DisplayHandle` → looks up `DisplayHandle`).

### MCP tools (12 total)

Existing (kept as-is):
- `list_repositories`
- `get_repository_context`
- `get_workspace_context`
- `search_code` (vector)
- `query_codebase` (alias)
- `get_symbol` (Chroma structural records)
- `search_code_hybrid` (vector + lexical + exact)

New:
| Tool | Returns |
|---|---|
| `get_definition(name/symbol, repo, path?, kind?)` | Symbol definitions + file vibe |
| `get_imports(repo?, path?)` | Import graph for a file or whole repo |
| `trace_export(name, repo?)` | Definition location + all files that import it |
| `list_symbols(repo?, kind?, path?)` | All symbols matching filters |
| `resolve_imports(repo, path?, rerun?)` | Resolved import chains (import → symbol) |

### Assembly label indexing (`syntax/indexer.py`)

Regex-based extraction for `.s` and `.inc` files (ca65-style 6502 assembly).
Finds label definitions (`label:`) and equate constants (`FOO = $xx`). Stored
as `CodeSymbol` with `kind="label"` or `kind="constant"` and
`language="assembly"`. No tree-sitter grammar needed.

### Language detection extension (`syntax/detection.py`)

Added `".s"` and `".inc"` → `"assembly"` to `LANGUAGE_BY_SUFFIX`.

## What's deferred (v2)

- Call-site captures and call graph (needs tree-sitter query additions for
  Python/Rust call expressions)
- `tokens_estimate` field in tool responses
- Language-specific docstring/vibe extraction (current vibe is just first 5
  symbol signatures — language-agnostic)
- Tests against seeded SQLite database

## Current state

Tested against compy6502 (embedded Rust + Python + ca65 assembly):
- 967 symbols (418 Python/Rust, 549 assembly labels)
- 126 imports, 45 resolved cross-file
- 84 file vibes
- All 5 new MCP tools verified working via stdio

## Risk

- **Cross-file resolution is language-dependent.** Python's `from X import Y`
  and Rust's `use X::Y` are tractable. Dynamic imports (Python `__import__`,
  Rust `#[path]`) are not. Start with the static cases.
- **Call-site extraction is heuristic.** Dynamic dispatch, method calls on
  trait objects, and higher-order functions won't resolve.
- **Tree-sitter grammar quality varies.** Rust and Python grammars are mature.
- **Assembly regex has edge cases.** Labels inside string literals or comments
  could produce false positives. So far no issues with ca65 style.
