# 56. dbt: read the live warehouse catalog in the plugin's commands

- **State:** done
- **Labels:** dbt, integrations
- **Created:** 2026-11-10
- **Related:** [50. dbt: read `catalog.json`](0050-dbt-catalog-json.md),
  [55. `sqlassert-dbt suggest`](0055-dbt-suggest-model-files.md)

`--catalog` (50) reads `catalog.json`: a snapshot with columns and types only.
A source table changed after `dbt docs generate` cannot be told apart, and the
catalog has no keys, nullability or collations.

Core's `DatabaseKnowledgeFetcher`s (`sqlassert/discovery/`) read all of that,
current, from a live connection: columns, types, nullability, primary keys,
unique sets, foreign keys, collations. `build`, `test` and `suggest` already
run dbt, so they could read the warehouse the target names, with no extra
credentials.

## Idea

An opt-in `--connect` on `build`, `test` and `suggest`: for each relation a
model reads that enters its Program as a table (source, seed, snapshot,
incremental model), fetch its metadata and declare it, instead of the
manifest's unknown columns or the catalog's. Like `analyze_database`, it may
add proofs, never change what the SQL and the manifest alone prove; keys the
warehouse does not enforce follow `trust_unenforced_keys` and its warnings.
`check` stays connectionless (it must run where dbt is not installed).

## Decision (2026-11-11): both ways in

- **Connection:** (c), both. `--connect` on `build`, `test` and `suggest`
  opens dbt's adapter connection from the profile and target dbt ran with
  (`sqlassert_dbt/connect.py`; `--target`, `--profile`, `--profiles-dir` and
  `--vars` after `--` are honoured) and closes it before dbt runs again (a
  DuckDB file takes one writer). `analyze_project(..., fetcher=...)` and
  `suggest_models(..., fetcher=...)` take any core fetcher.
- **Adapter to fetcher:** DuckDB (dbt-duckdb's cursor), Postgres, Redshift,
  Snowflake, MySQL and Trino (the driver's DB-API connection). BigQuery and
  Databricks are not mapped: their handles are not what the fetchers take, so
  `--connect` says to pass a fetcher from Python.
- **Declaring the facts:** reuse core: `hydrate` then `bind` on the Program
  the plugin assembles (`DatabaseKnowledgeFetcher.convert`, shared with
  `analyze_database`), so enforcement and
  `trust_unenforced_keys` behave as in `analyze_database`.
- **Precedence:** under a connection the plugin reads no catalog. An
  enforced contract's table is still declared from the contract, since the
  build may not have made it yet (a connection only adds proofs); every other
  table comes from the warehouse. A warehouse view is declared by its stored query.
- **Previous runs:** an incremental model's `{{ this }}` stays unknown; its
  not-found Diagnostic is dropped.
- **Tests:** `integrations/dbt/tests/test_connect.py`, DuckDB end to end.
  dbt-duckdb keeps its file open past dbt's cleanup (`keep_open`), so
  `--connect` closes it, and it replaces an adapter dbt registered earlier
  in the process.

## Open questions (answered above)

- **Getting the connection.** (a) Through dbt's adapter, from the profile and
  target dbt just ran with (`adapter.connections....handle`, the driver's own
  connection): no extra setup, but it leans on dbt's internal adapter API,
  which changes across versions. (b) Python API only:
  `analyze_project(..., fetcher=...)`, nothing on the command line. (c) Both.
- **Adapter to fetcher.** Map each adapter to its fetcher (DuckDB, Postgres,
  Redshift, Snowflake, BigQuery, Databricks, MySQL, Trino) and check that the
  handle each adapter exposes is what that fetcher expects (DuckDB takes
  anything with `execute`; others want a DB-API cursor).
- **Declaring the facts.** The plugin writes its Program as text
  (`CREATE TABLE`), core hydration binds `DatabaseRelationMetadata` to the IR
  (`fetcher.bind`). Reuse `bind` (with the plugin's relation names), or write
  keys and NOT NULL into the `CREATE TABLE` text and lose the enforcement
  distinction?
- **Precedence.** Connection over catalog over manifest columns; an enforced
  contract that disagrees with the warehouse: warn, as with the catalog?
- **Cost.** One round of metadata queries per relation reached, memoized
  across models; a large project reads many sources.
- **Testing.** The `built` fixture (conftest.py) is a DuckDB file, so the
  DuckDB path is testable end to end; the rest only through the dialect
  certifier's engines, if at all.
