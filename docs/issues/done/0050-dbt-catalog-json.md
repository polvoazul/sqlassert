# 50. dbt: read `catalog.json`

- **State:** done
- **Labels:** dbt, integrations, P2
- **Created:** 2026-11-08

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

## Done

`--catalog PATH` on `check`, `build`, `test` and `suggest` (and
`catalog_path=` on `analyze_project` and `suggest_models`) reads
`catalog.json` (schema v1, `load_catalog`). A table no enforced contract types
completely enters the Program as a `CREATE TABLE` of the catalog's columns, in
order, typed where SQLGlot reads the warehouse's type and untyped otherwise.
Relations that enter as their query (views, tables rebuilt each run) are
unaffected.

Staleness, answered: a catalog cannot be dated against the warehouse, so only
what the project itself shows is checked. An entry is not used, with a
warning, when its database.schema.name is not the manifest's relation name
(another target, `dbt-catalog-other-relation`, one warning for all) or when a
seed's CSV header lists other columns (`dbt-catalog-stale`). An enforced
contract always wins, with a `dbt-catalog-stale` warning when the catalog
lists other columns. A source changed after `docs generate` is undetectable
without a connection; that is the case for reading the live catalog instead
(follow-up). Age against the manifest was dropped: every `dbt compile`
rewrites the manifest, so it would always warn.

Tests build the test project into a DuckDB file (`built` in conftest.py) and
run a real `dbt docs generate`.
