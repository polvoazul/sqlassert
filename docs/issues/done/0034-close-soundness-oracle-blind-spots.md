# 34. Close the soundness oracle's blind spots

- **State:** done
- **Labels:** ready-for-agent
- **Created:** 2026-11-01

## Problem

`pytest --soundness-oracle` skips some PROVED assertions, so they are never
judged against DuckDB:

- **5 programs DuckDB rejects.** Two self-join a view without aliases
  (`FROM per_user JOIN per_user USING (user_id)` in
  `tests/test_analyze_aggregate_and_distinct.py`); three Foreign Key tests use
  a missing schema (`app.`), a CTE as the referenced table, or a view that
  references itself.
- **Foreign Keys onto a CTE.** The oracle cannot create a constraint onto a
  CTE, so those propagation cases (issue 20) are checked by tests only.

## What to build

- Rewrite the five test programs so DuckDB accepts them without changing what
  they test (add aliases, create the schema), or have the oracle run a
  DuckDB-compatible rewrite of them.
- Judge Foreign Keys onto a CTE with a coverage query (`NOT EXISTS` against the
  CTE) instead of a constraint.

## Acceptance criteria

- [x] The oracle's skip count is 0, or each remaining skip is listed with why.
- [x] 0 counterexamples.

## Done

The oracle does not rewrite programs: each test's SQL is runnable on DuckDB
as written. The two `per_user` self-joins got aliases; the self-referencing
view test creates `plain_tree`; the qualified-names test runs
`CREATE SCHEMA app` itself; the "UNIQUE plus SET NOT NULL" amendment sets
NOT NULL before adding the UNIQUE (DuckDB cannot ALTER a column a unique
index depends on). The one oracle fix is a bug: a checked Select's own CTEs
now prefix its violation query, so a Foreign Key onto a CTE is judged by its
`NOT EXISTS` coverage query.

Remaining skips, each by design: the four programs in
`tests/test_connection_warnings.py` that read tables with no `CREATE` (they
test analysis without a connection, so DuckDB has no table to run them on).

Finding: sqlassert reports `CREATE SCHEMA` as `unsupported-create-statement`
(`test_create_schema_is_reported_and_dropped`), so the qualified-names test
now expects that one diagnostic. Accepting it silently, like a plain index
(it states nothing about rows), is a product decision left open.
