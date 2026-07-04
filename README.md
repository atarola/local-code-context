# local-code-context

A static-analysis MCP server for multi-repo code understanding. Uses tree-sitter
to extract symbols and imports into a SQLite cross-reference database, then
serves deterministic results over stdio — no embeddings, no vector search, no
external services.

## How it works

1. `code-context-index` walks each repo with tree-sitter, extracts symbols
   (functions, classes, constants) and imports, and writes them to
   `xref.sqlite` alongside a content-hash manifest for incremental re-indexing.
2. `code-context-mcp` reads the xref database and exposes an MCP server over
   stdio with tools for symbol lookup, import chains, export tracing, and
   repository context assembly.

No embedding model, no vector database, no daemon. The only dependency is
SQLite, which Python includes in its standard library.

## Quick start

```bash
# Index a repo
uv run python -m local_code_context.indexing.indexer --repo /path/to/repo --db ./codebase_index

# Start the MCP server
uv run python -m local_code_context.mcp.server --db ./codebase_index
```

## MCP tools

| Tool | What it returns |
|---|---|
| `list_repositories` | All indexed repository names |
| `get_repository_context(repo, max_chars?)` | File tree, README, manifests, modules, tests, excerpts |
| `get_workspace_context(repos?, max_chars_per_repo?)` | Compact context for multiple repos |
| `get_definition(symbol, repo?, path?, kind?, limit?)` | Symbol definitions with file location and file vibe |
| `get_imports(repo?, path?, limit?)` | Import graph entries |
| `trace_export(name, repo?)` | Definition + all files that import it |
| `list_symbols(repo?, kind?, path?, limit?)` | All symbols matching filters |
| `resolve_imports(repo, path?, rerun?)` | Resolved import chains (import → symbol) |
| `trace_callers(callee, repo?)` | All call sites calling a function/method |
| `find_callers(symbol_id, limit?)` | Call sites resolved to a specific symbol ID |
| `find_callees(caller_symbol_id, include_unresolved?, limit?)` | Calls from a specific caller symbol |
| `find_calls_by_name(repo, callee_name, path?, limit?)` | Calls by callee name with resolved details |

The server starts instantly — no model loading, no network calls.

## Indexing

```bash
uv run python -m local_code_context.indexing.indexer \
  --repo /path/to/service-a \
  --repo /path/to/service-b \
  --db ./codebase_index
```

Or index all Git repos under a workspace directory:

```bash
uv run python -m local_code_context.indexing.indexer \
  --workspace /path/to/code \
  --db ./codebase_index
```

Re-run any time to refresh. A `manifest.json` inside `--db` tracks content
hashes per file; unchanged files are skipped. `xref.sqlite` is updated
incrementally.

The indexer skips `.git`, `.venv`, `node_modules`, `build`, `target`, and
common cache directories. Files matched by `.gitignore` are excluded in Git
repositories. Add a `.index_ignore` file to a repo for additional ignore
patterns.

## Watcher

```bash
uv run python -m local_code_context.indexing.watcher \
  --workspace /path/to/code \
  --db ./codebase_index
```

Auto-reindexes files on save. The watcher runs an initial full index on start,
then watches for filesystem changes.

## Nix

```bash
nix run .#index -- --workspace /path/to/code --db ./codebase_index
nix run .#mcp -- --db ./codebase_index
nix run .#watch -- --workspace /path/to/code --db ./codebase_index
```

### Home Manager module

```nix
{
  inputs.local-code-context = {
    url = "github:atarola/local-code-context";
    inputs.nixpkgs.follows = "nixpkgs";
  };
}

# In home.nix:
{
  imports = [ inputs.local-code-context.homeManagerModules.default ];

  services.local-code-context = {
    enable = true;
    workspaces = [ "/home/your-user/code" ];
    db = "/home/your-user/.local/share/local-code-context/codebase_index";
    autoStart = false;
  };
}
```

Start and stop the watcher with:

```bash
code-context-up
code-context-status
code-context-down
code-context-logs
```

## Schema

`xref.sqlite` tables:

| Table | Contents |
|---|---|
| `symbols` | Name, kind, language, path, repo, line range, parent, signature |
| `imports` | Source module, imported name, path, repo, start line |
| `file_vibe` | Per-file summary derived from first 5 symbol signatures |
| `repo_meta` | Name, root path, last indexed |
| `resolved_imports` | Maps `import_id` → `symbol_id` (post-indexing resolution) |
| `call_sites` | Caller/callee name, qualifier, source ranges, caller language, resolution status, resolved symbol ID |
