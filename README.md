# Local multi-repo code RAG

A minimal retrieval setup so a 12GB-VRAM local model can reason across
multiple codebases without needing them all loaded into its context window
at once. It embeds and retrieves relevant chunks first, then only sends
those chunks to the model.

## How it works

1. `local_code_rag.index_repos` walks each repo you point it at, splits files into
   roughly 60-line chunks, embeds each chunk with a small local embedding
   model (`nomic-embed-text`), and stores them in a local Chroma DB tagged
   with repo name, file path, and line range.
2. `query.py` embeds your question, retrieves the most relevant chunks across
   all indexed repos, and sends just those chunks, with citations, to your
   coding model, for example `qwen2.5-coder:14b`.

This keeps VRAM usage low: the embedding model is small, and the chat model
only sees a few thousand tokens of retrieved context instead of whole repos.

## Setup

```bash
ollama pull nomic-embed-text
ollama pull qwen2.5-coder:14b

uv sync
```

## Nix

Run commands directly from the flake:

```bash
nix run .#index -- --repo /path/to/service-a --repo /path/to/service-b --db ./codebase_index
nix run .#query -- --db ./codebase_index --q "Where is retry handled?"
nix run .#watch -- --repo /path/to/service-a --db ./codebase_index
```

Install from another flake by adding this repo as an input and using
`inputs.local-code-rag.packages.${system}.default`.

The flake also exports a NixOS module:

```nix
{
  inputs.local-code-rag.url = "github:atarola/local-code-rag";
}
```

Then import and configure it from your NixOS configuration:

```nix
{ inputs, ... }:

{
  imports = [
    inputs.local-code-rag.nixosModules.default
  ];

  services.local-code-rag = {
    enable = true;
    user = "your-user";
    group = "users";
    repos = [
      "/home/your-user/code/service-a"
      "/home/your-user/code/service-b"
    ];
    db = "/home/your-user/.local/share/local-code-rag/codebase_index";
    collection = "code_chunks";
    embedModel = "nomic-embed-text";
    ollamaUrl = "http://127.0.0.1:11434";

    # Keep this false if you only want the watcher running when you start it.
    autoStart = false;
  };
}
```

With `autoStart = false`, start it manually when Ollama is running:

```bash
sudo systemctl start local-code-rag-watch
sudo systemctl status local-code-rag-watch
sudo systemctl stop local-code-rag-watch
```

If you are running in a Nix shell and Chroma/NumPy fails to import with missing
shared libraries such as `libstdc++.so.6` or `libz.so.1`, enter the provided
shell first:

```bash
nix-shell
uv sync
```

The Nix shell also provides helpers for a systemd-managed Ollama service:

```bash
ollama-up        # sudo systemctl start ollama
ollama-status    # service state plus recent logs
ollama-down      # sudo systemctl stop ollama
```

This keeps Ollama off your GPU until you explicitly start it for a code-RAG
session.

## Usage

Index each codebase. Repeat `--repo` for as many repos as you want in the same
DB:

```bash
uv run code-rag-index --repo /path/to/service-a --repo /path/to/service-b --db ./codebase_index
```

Re-run against a repo any time to refresh it after code changes. Indexing is
incremental: `manifest.json` inside the `--db` directory tracks a content hash
per file, so unchanged files are skipped entirely on re-runs. Only new, edited,
and deleted files are reflected in Chroma.

Use `--force` to ignore the manifest and re-embed everything, for example after
changing `CHUNK_LINES` or `CHUNK_OVERLAP` in `src/local_code_rag/index_repos.py`.

Ask a question that spans repos:

```bash
uv run code-rag-query \
  --db ./codebase_index \
  --q "Where does service-a's retry logic call into service-b's client, and could that cause the duplicate-write bug we're seeing?"
```

Restrict to one repo for a single-codebase review:

```bash
uv run code-rag-query --db ./codebase_index --repo service-a --q "Review the error handling in the payment module"
```

Only inspect retrieval hits without calling the chat model:

```bash
uv run code-rag-query --db ./codebase_index --q "Where is retry handled?" --no-answer
```

Print retrieved chunks before the model answer:

```bash
uv run code-rag-query --db ./codebase_index --q "Where is retry handled?" --show-context
```

## Tuning notes

- `CHUNK_LINES` and `CHUNK_OVERLAP` in `src/local_code_rag/index_repos.py`: 60 and 10 are
  reasonable defaults for most languages. Drop to about 30 lines for dense code
  and raise for verbose config-heavy files.
- `--top-k` in `query.py`: more chunks means more cross-file context but also a
  larger prompt. Start at 10; raise to 15-20 for genuinely cross-repo questions.
- Set `num_ctx` on your Ollama chat model so it does not truncate retrieved
  context:

```text
ollama run qwen2.5-coder:14b
/set parameter num_ctx 16384
/save qwen2.5-coder-16k
```

Then query with:

```bash
uv run code-rag-query --model qwen2.5-coder-16k --db ./codebase_index --q "..."
```

## Privacy

The Chroma DB contains source chunks and metadata from whatever repos you
index. Treat it like source code: do not commit `codebase_index/`, `.chroma/`,
or any custom DB directory that contains an index of private code.

By default, embeddings and chat requests go to a local Ollama endpoint at
`http://localhost:11434`. The tool does not call a hosted model API unless you
explicitly point `--ollama-url` at one.
