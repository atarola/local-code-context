# local-code-context

Index your code once, then ask an MCP client questions that return only the
relevant context. local-code-context builds a local cross-reference database
from symbols, imports, and call sites, so you can answer questions like:

- Where is this defined?
- Who imports it?
- What calls it?
- What does this repo look like from the outside?

## Why use it

- Fast answers from a local SQLite index
- Smaller prompts and more context budget from focused context slices
- Works locally, with no embeddings or external services
- Gives agents deterministic code context instead of guesses
- Reindexes incrementally, so repeated runs stay cheap

## Quick start

1. Index your repo or workspace.
2. Start the MCP server.
3. Point your client or agent at the server over stdio.

```bash
uv run code-context-index --repo /path/to/repo --db ./codebase_index
uv run code-context-mcp --db ./codebase_index
```

If you want to index a workspace of Git repos instead:

```bash
uv run code-context-index --workspace /path/to/code --db ./codebase_index
```

## Nix setup

If you prefer Nix, the same commands are exposed as flake apps:

```bash
nix run .#index -- --repo /path/to/repo --db ./codebase_index
nix run .#mcp -- --db ./codebase_index
nix run .#watch -- --workspace /path/to/code --db ./codebase_index
```

For a persistent watcher, use the Home Manager module:

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

## What it answers

| Question | Tool |
|---|---|
| "What repos are indexed?" | `list_repositories` |
| "What is in this repo?" | `get_repository_context(repo, max_chars?)` |
| "What context is available across repos?" | `get_workspace_context(repos?, max_chars_per_repo?)` |
| "Where is this symbol defined?" | `get_definition(symbol, repo?, path?, kind?, limit?)` |
| "What imports does this file have?" | `get_imports(repo?, path?, limit?)` |
| "Who imports this export?" | `trace_export(name, repo?)` |
| "What symbols are in this repo?" | `list_symbols(repo?, kind?, path?, limit?)` |
| "Where does this import resolve?" | `resolve_imports(repo, path?, rerun?)` |
| "Who calls this function?" | `trace_callers(callee, repo?)` |

## How it works

1. `code-context-index` walks each repo with tree-sitter, extracts symbols
   (functions, classes, constants) and imports, and writes them to
   `xref.sqlite` alongside a content-hash manifest for incremental re-indexing.
2. `code-context-mcp` reads the xref database and exposes an MCP server over
   stdio with tools for symbol lookup, import chains, export tracing, and
   repository context assembly.

No embedding model, no vector database.

The server starts instantly, with no model loading and no network calls.

## Indexing

```bash
uv run code-context-index \
  --repo /path/to/service-a \
  --repo /path/to/service-b \
  --db ./codebase_index
```

Or index all Git repos under a workspace directory:

```bash
uv run code-context-index \
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
uv run code-context-watch \
  --workspace /path/to/code \
  --db ./codebase_index
```

Auto-reindexes files on save. The watcher runs an initial full index on start,
then watches for filesystem changes.

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
