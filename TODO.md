# Storage TODO

The core storage layer is on SQLAlchemy 2.x and the indexer MVE is complete. This file tracks the remaining storage work that improves maintainability, diagnostics, and structural-query capability without reopening the persistence migration.

## P0 — Close the storage migration

- [ ] Remove remaining production `sqlite3` access from `indexing/indexer.py`.
  - Use the mapped `RepoMeta` model and shared session factory.
  - Keep `repo_meta` updates inside the owning indexing transaction where practical.
  - Do not publish `last_indexed` for a failed index run.

- [ ] Remove remaining production `sqlite3` access from `mcp/context.py`.
  - Add a shared ORM query for repository metadata.
  - Reuse the same deterministic ordering as `list_repositories`.

- [ ] Retire `storage/schema.py`.
  - Replace `open_db()` callers with the SQLAlchemy engine/session helpers.
  - Replace `ensure_schema()` callers with `db.schema.ensure_orm_schema()`.
  - Delete duplicate schema definitions after all callers migrate.
  - Keep `db/models.py` and `db/schema.py` as the only schema authorities.

- [ ] Migrate test setup from legacy `open_db()` / `ensure_schema()` helpers.
  - Provide one file-backed temporary SQLite fixture.
  - Initialize through the production ORM schema path.
  - Dispose engines cleanly during teardown.

- [ ] Add an architectural test that prevents new production `sqlite3` imports.
  - Allowlist only explicit compatibility or migration code, if still required.

## P1 — Storage health and diagnostics

- [ ] Add a storage health API and CLI command.

  Suggested command:

  ```bash
  code-context-index check --db ./codebase_index
  ```

  Report:

  - schema version;
  - SQLite version;
  - database path and size;
  - indexed repositories and files;
  - row counts per table;
  - unresolved and ambiguous import counts;
  - unresolved and ambiguous call counts;
  - stale repository roots;
  - manifest/database disagreements;
  - `PRAGMA foreign_key_check`;
  - `PRAGMA integrity_check`.

- [ ] Add machine-readable health output.

  ```bash
  code-context-index check --db ./codebase_index --json
  ```

- [ ] Add reusable integrity functions rather than embedding checks in the CLI.
  - `check_foreign_keys()`
  - `check_integrity()`
  - `check_manifest_consistency()`
  - `get_storage_stats()`

- [ ] Run foreign-key and integrity checks in mutation-heavy integration tests.

## P1 — Schema lifecycle

- [ ] Formalize the schema-version contract.
  - New database initialization.
  - Supported upgrade paths.
  - Rejection of unknown future schema versions.
  - Idempotent reopening of the current schema.
  - Clear policy for rebuild-versus-migrate decisions.

- [ ] Add a committed pre-upgrade database fixture.
  - Verify v1 → current migration.
  - Verify data preservation.
  - Verify indexes and foreign keys after migration.
  - Verify migration is idempotent.

- [ ] Document downgrade policy.
  - Prefer “unsupported; rebuild the derived index” unless a real downgrade need appears.

- [ ] Add an extractor/index format version.
  - Invalidate unchanged-file entries when parser queries, normalization, or derived-record rules change.
  - Do not rely on source content hashes alone.

## P1 — Transaction and session ownership

- [ ] Centralize the session factory.
  - Avoid creating a new engine and `sessionmaker` inside every storage function.
  - Provide one supported engine/session construction path per database.

- [ ] Make transaction ownership explicit.
  - Low-level storage functions should accept a `Session` when participating in a larger operation.
  - Convenience wrappers may open their own transaction for isolated calls.
  - Never commit from a helper when the caller owns the transaction.

- [ ] Add session-aware variants for path mutation.

  Example shape:

  ```python
  def replace_file_xref(
      session: Session,
      repo: str,
      path: str,
      extraction: Extraction,
  ) -> None:
      ...
  ```

- [ ] Keep manifest publication outside the database transaction but only after commit succeeds.

- [ ] Test rollback at each mutation phase.
  - delete prior rows;
  - insert symbols;
  - insert imports;
  - insert calls;
  - rebuild relationships.

## P1 — Path replacement cleanup

- [ ] Deduplicate path-deletion logic.
  - `delete_file_xref()` and `index_file_xref()` should call one internal dependency-safe deletion helper.

- [ ] Keep deletion ordering documented and tested.

  Required logical order:

  1. call sites originating in the path;
  2. resolved-import rows tied to path imports;
  3. imports;
  4. external call resolutions targeting path symbols;
  5. symbols;
  6. file profile/vibe.

- [ ] Ensure empty, comment-only, and import-only files remain valid replacements.

- [ ] Ensure calls in other files cannot retain stale `resolved_symbol_id` values after target deletion.

- [ ] Return mutation statistics.
  - deleted rows by type;
  - inserted rows by type;
  - relationships invalidated.

## P2 — Query API consolidation

- [ ] Define small typed result records for public storage queries.
  - Avoid leaking ORM entities across session boundaries.
  - Keep MCP rendering independent of SQLAlchemy object state.

- [ ] Consolidate `_orm_to_dict()` usage.
  - Prefer explicit serializers per public record type.
  - Avoid accidental exposure of internal columns or relationships.

- [ ] Require explicit deterministic ordering for every multi-row query.
  - Add stable tie-breakers.
  - Do not rely on primary-key order unless it is part of the contract.

- [ ] Add pagination primitives for large result sets.
  - Keep existing `limit` behavior.
  - Consider stable cursor pagination only when real repositories require it.

- [ ] Add unified reference queries:
  - definition references;
  - resolved imports;
  - resolved calls;
  - unresolved same-name calls, clearly separated.

- [ ] Add graph-query helpers:
  - direct callers;
  - direct callees;
  - importers;
  - dependency paths;
  - bounded transitive traversal.

## P2 — Resolution quality

- [ ] Return combined import and call resolution statistics from `resolve_repo_relationships()`.

- [ ] Persist or expose resolution provenance.
  - same-file;
  - direct import;
  - qualified import;
  - current-class method;
  - explicit Rust module path;
  - unresolved;
  - ambiguous;
  - external.

- [ ] Add candidate-count diagnostics for ambiguous edges.

- [ ] Keep resolution rebuilds idempotent and deterministic.

- [ ] Add targeted incremental invalidation after correctness is proven.
  - Start with repository-wide rebuilds as the safe baseline.
  - Optimize only with equivalence tests against full resolution rebuilds.

- [ ] Add cross-repository resolution when imports uniquely target another indexed repository.

## P2 — File profiles

- [ ] Rename `file_vibe` to `file_profile` when a schema change is next justified.

- [ ] Replace “first five signatures” with a deterministic structural profile derived from:
  - path role;
  - exported symbols;
  - imports;
  - importers;
  - callers and callees;
  - entry-point status;
  - test status.

- [ ] Store a profile version and dependency fingerprint.
  - Recompute when relational facts change, not only when file contents change.

- [ ] Keep profile claims traceable to indexed facts.

## P2 — Repository identity and paths

- [ ] Introduce a stable internal repository ID.
  - Keep display name and root path as mutable metadata.
  - Handle duplicate directory names, moved repositories, worktrees, and symlinks.

- [ ] Normalize all stored paths as repository-relative POSIX-style paths.

- [ ] Add uniqueness constraints for logical file identity.

- [ ] Test Windows drive letters, case behavior, Unicode paths, and symlink boundaries.

## P2 — Concurrency and performance

- [ ] Add a documented single-writer policy.
  - Prevent watcher and manual indexer writes from interleaving.
  - Provide a clear lock/busy error.

- [ ] Confirm and document WAL and busy-timeout behavior.

- [ ] Reuse engines rather than creating and disposing one per query or file mutation.

- [ ] Add storage benchmarks:
  - cold full index;
  - no-op reindex;
  - one-file replacement;
  - central-symbol deletion;
  - full relationship rebuild;
  - MCP read latency.

- [ ] Track database-size growth and index effectiveness.

- [ ] Review indexes using realistic `EXPLAIN QUERY PLAN` output.
  - Keep only indexes that support observed query paths.

## P3 — Optional extensions

- [ ] Add index snapshot/export support for regression comparison.

- [ ] Add a compact database-inspection command for debugging selected rows.

- [ ] Add impact-analysis queries over imports and calls.

- [ ] Add assembly relationships to the same storage model:
  - labels;
  - constants;
  - includes;
  - macros;
  - operand references.

- [ ] Add reference kinds beyond calls:
  - type references;
  - inheritance;
  - trait/interface implementation;
  - constant use;
  - re-exports.

## Definition of done

Storage can be considered mature when:

- [ ] Production code has one schema authority and one engine/session path.
- [ ] No unapproved production module imports `sqlite3` directly.
- [ ] Every write operation has an explicit transaction owner.
- [ ] Path replacement and deletion share one dependency-safe implementation.
- [ ] Schema upgrades and future-version rejection are integration-tested.
- [ ] Health checks detect foreign-key, integrity, and manifest inconsistencies.
- [ ] Public queries return typed detached records in deterministic order.
- [ ] Resolution rebuilds are idempotent and expose useful diagnostics.
- [ ] Clean rebuild and incremental mutation remain logically equivalent.
- [ ] Storage performance is measured on at least one realistic multi-repository fixture.

