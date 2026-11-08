# 50. dbt: read `catalog.json`

- **State:** not planned
- **Labels:** dbt, integrations
- **Created:** 2026-11-08
- **Parent:** [44. v1 external integrations](0044-v1-external-integrations.md)

`sqlassert-dbt` reads only `manifest.json`, so the columns of sources, seeds,
snapshots and uncontracted models are unknown (a `dbt-unknown-columns`
warning): `SELECT *` over them cannot be expanded, an unqualified column may
be ambiguous, and their types are unknown, so no equality pins a column
through an implicit cast.

`dbt docs generate` writes `target/catalog.json` from the warehouse's
information schema: every relation's columns and types, but no keys and no
nullability.

## Idea

An opt-in `--catalog PATH` (default off) that reads `catalog.json` as a
catalog: column lists and types for relations the manifest leaves
incomplete. It comes from a connection, so like `analyze_database` it may add
knowledge, and so proofs, but never change what the SQL and the manifest
alone prove.

## Open questions

- Staleness: the catalog is a snapshot of the last `docs generate`, possibly
  of another target. Warn when it is older than the manifest, or when its
  relation names do not match the manifest's?
- Should a type it gives for a model the manifest also types (a contract)
  ever disagree, which wins? Most likely: the contract, with a warning.
