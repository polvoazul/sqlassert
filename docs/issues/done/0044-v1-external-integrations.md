# 44. v1 external integrations

- **State:** done (2026-11-08)
- **Labels:** epic, needs-design (child of 43)
- **Created:** 2026-11-08
- **Parent:** [43. External integrations](../0043-external-integrations.md)

The first integrations: a `check` command for CI, the dialects dbt users run,
and dbt itself. dbt depends on both: it needs `check`'s exit codes and
output, and most dbt projects are on a warehouse, not DuckDB.

## `check` CLI

Today assertions are checked only from Python (`analyze()` in a test);
`sqlassert suggest` lists what could be asserted but checks nothing.

- [x] `sqlassert check <files/globs>`: analyze each SQL Program and exit
  non-zero when any assertion is `UNKNOWN` or a Diagnostic is reported
  (warnings never fail it, as in `Report`).
- [x] Config in `pyproject.toml` (`[tool.sqlassert]`): dialect, paths, codes to
  ignore. Command-line flags override it.
- [x] Output formats: text (default), JSON, SARIF (GitHub code scanning),
  GitHub `::error` annotations.
- [x] Column-precise spans on `Origin` (today: line only), so annotations land
  on the marker.
- [x] `--explain`: print each `UNKNOWN`'s evidence, as `suggest --explain` does.

## Dialects

Today only DuckDB is tested, and the soundness oracle judges DuckDB programs
only. For each dialect:

- [x] Builtin registry entries (`convert._builtins`): strict, total and
  injective flags as that dialect defines them.
- [x] NULL, type and comparison semantics the rules depend on: implicit casts,
  default collation (ENUM narrowing), and arithmetic that can return NULL.
- [x] Catalog fetcher (columns, nullability, keys, Foreign Keys, views).
  Postgres (live-tested) and MySQL; Snowflake, BigQuery, Databricks and
  Redshift (scripted fakes only) since [47](0047-discovery-base-class.md),
  whose catalog keys are declarations under `trust_unenforced_keys` (on by default).
- [x] Test cases in that dialect, and a way to judge soundness without DuckDB
  (a real engine in CI, or an argued mapping onto DuckDB semantics).

Dialects (all implemented; Postgres and MySQL run against real engines, the
warehouses against docs and scripted fakes only, validated in
[51](../0051-validate-snowflake-against-a-real-engine.md),
[52](../0052-validate-bigquery-against-a-real-engine.md),
[53](../0053-validate-databricks-against-a-real-engine.md) and
[54](../0054-validate-redshift-against-a-real-engine.md)):

- [x] **Postgres.** Closest to DuckDB; enforced keys.
- [x] **MySQL.** Default collations are case-insensitive, so distinct strings
  may compare equal; `||` is OR unless `PIPES_AS_CONCAT`; loose `GROUP BY` under
  some SQL modes; arithmetic can return NULL.
- [x] **Snowflake.** Primary, unique and foreign keys are not enforced
  (`RELY` marks them trusted); NOT NULL is.
- [x] **BigQuery.** Primary and foreign keys are not enforced.
- [x] **Databricks / Spark.** Primary and foreign keys are informational
  (`RELY`); NOT NULL and CHECK are enforced.
- [x] **Redshift.** Primary, unique and foreign keys are not enforced, though
  the planner trusts them.

Decide: is an unenforced key Declared always, only with `RELY`, or only when
the user opts in? It must never count as proved (ADR 0005). Implemented,
decided 2026-11-08: `trust_unenforced_keys` is on by default and declares
every unenforced key; with it off, only `RELY` (Snowflake, Databricks / Spark)
does and any other gets an `unenforced-constraint` warning
([docs/dialects.md](../../dialects.md)).

## dbt

- [x] Read `manifest.json` and the compiled SQL: each model becomes a view,
  each source a table, seeds and snapshots tables with their tests.
- [x] Map dbt tests and constraints onto Properties:
  - `unique` → Unique Set (nulls exempt, as ours)
  - `not_null` → Not Null
  - `relationships` → Foreign Key (null child rows exempt, MATCH SIMPLE)
  - `accepted_values` → ENUM (NULL passes, as ours)
  - `dbt_utils.unique_combination_of_columns` → Unique Set (NULL equal to NULL,
    stronger; fine as a declaration)
  - model contract `primary_key`, `foreign_key` and `not_null` constraints
- [x] A model's own tests are assertions for that model and declarations only
  for its downstream models. Otherwise every test proves itself.
- [x] Weaker tests are not facts: a `where:` config, `severity: warn`,
  `store_failures` thresholds, tests on incremental models scoped to new rows.
- [x] Report: tests statically proved (the test can be skipped, saving
  warehouse time), tests not proved (coverage the runtime test still carries),
  and Properties proved but untested (offered as `schema.yml` additions).
- [x] Markers written in model files survive Jinja compilation, but line
  numbers don't: map each finding back to the model's source file and line.
- [x] Ephemeral models are inlined as CTEs; incremental models have two
  `is_incremental()` branches, and both must be analyzed.
- [x] A command (`sqlassert-dbt [project dir]`, the plugin of #48) that reuses `check`'s exit codes,
  config and output formats, for CI. Exit codes shared; text output only, no
  `[tool.sqlassert]` config yet (moved to 43's backlog).

## Open questions

- Skipping proved tests: only report them, or also emit a selector or
  exclusion list dbt can use?
- dbt Fusion (the Rust engine) and dbt Cloud: is `manifest.json` still the
  stable interface for both?
