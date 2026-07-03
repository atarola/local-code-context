Add an MCP tool named `list_repositories`.

Before editing, inspect the repository and identify:

1. how the existing Chroma client and collection are constructed
2. how MCP tools are registered and dispatched
3. the metadata schema used by indexed records
4. the existing response helpers used by MCP tools

Do not invent filesystem metadata files, repository registries, or alternate storage.

## Tool behavior

The tool takes no required arguments.

It must derive repositories directly from metadata in the existing Chroma collection.

For each repository, return:

* repository name
* total record count
* structured file count
* fallback file count
* mixed file count
* languages present
* record counts grouped by `chunk_type`

Definitions:

* a structured file is a distinct `(repo, path)` pair with a `file_map` record
* a fallback file is a distinct `(repo, path)` pair with a `text` record
* count files, not chunks
* multiple text chunks from one file count as one fallback file
* symbol and `symbol_part` records do not affect file counts
* a mixed file has both `file_map` and `text` records
* languages come from non-empty `language` metadata
* unlabeled records must not create a fake language name

## Architecture constraints

* reuse the exact existing Chroma configuration and collection-construction path
* do not hard-code database paths or collection names
* use metadata retrieval only; do not perform embedding search
* use `collection.get(...)` or the existing equivalent for full metadata enumeration
* keep the implementation language-neutral
* do not add `if language == ...` branches
* do not alter the index schema
* do not change indexing or extraction behavior
* never write logs or diagnostics to MCP stdout
* preserve newline-delimited JSON-RPC transport
* keep `server.py` limited to import, tool schema, and dispatch

Use a focused module such as:

```text
src/local_code_context/mcp/repositories.py
```

## Aggregation requirements

Per repository, track conceptually:

```python
{
    "record_count": 0,
    "record_types": Counter(),
    "structured_files": set(),
    "fallback_files": set(),
    "languages": set(),
}
```

For every metadata record:

* increment `record_count`
* increment the matching `chunk_type` count
* add `path` to `structured_files` for `file_map`
* add `path` to `fallback_files` for `text`
* add non-empty language values to `languages`

Mixed files are:

```python
structured_files & fallback_files
```

Sort deterministically:

* repositories by name
* languages alphabetically
* record types alphabetically

Do not apply an arbitrary result limit that could silently omit repositories. If pagination is required, fetch all records safely.

## Empty collection behavior

Return a direct result such as:

```text
No repositories are currently indexed.
```

Do not ask the user whether they intended to index repositories.

## Tests

Add tests covering:

1. empty collection
2. one repository
3. multiple repositories
4. deterministic repository ordering
5. distinct structured-file counting
6. symbol records not inflating file counts
7. multipart symbols not inflating file counts
8. multiple text chunks from one file counted once
9. mixed structural/text file detection
10. unlabeled text records
11. multiple languages in one repository
12. record-type counts
13. deterministic record-type ordering
14. raw MCP dispatch and response shape
15. no stdout output outside the MCP response

Use a fake or temporary collection. Do not depend on the real local index.

## Verification

Run:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python -m unittest discover -s tests -p 'test_*.py'
nix flake check
nix build .#default
```

Then invoke the tool against the real local index and report the exact raw MCP result.

Before implementation, provide a short plan naming the actual existing modules and functions you will reuse. Do not ask whether to proceed.

Do not add any unrelated feature or refactor.

