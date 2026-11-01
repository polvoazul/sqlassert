# 30. Read enum types and collations from the catalog

- **State:** done
- **Labels:** ready-for-agent
- **Created:** 2026-11-01

## Problem

The DuckDB adapter (`sqlassert/discovery/duckdb.py`) reads columns,
nullability, keys and Foreign Keys, but not:

- **Enum types.** A column of DuckDB type `ENUM('new', 'paid')` has a known
  Enumerated Domain, but no `EnumeratedDomain` Knowledge is bound for it.
- **Collations.** Catalog columns never count as plain text (`binary_text`,
  issue 29) because the catalog listing drops the collation. So string ENUM
  reasoning on hydrated tables is always conservative.

## Acceptance criteria

- [x] A catalog column of an enum type yields `EnumeratedDomain` Knowledge with
  its members as string values.
- [x] A catalog `VARCHAR` column with no collation is marked `binary_text`; one
  with a collation is not. If DuckDB cannot report collation reliably, document
  that and keep the conservative default.
- [x] Tests against an in-memory DuckDB connection.

## Done

`DatabaseColumn` carries `data_type` and `collation`; hydration declares
each catalog column with them, so the Program compares and enumerates a
catalog column exactly as it would one declared in the SQL. The DuckDB
adapter reads types from `duckdb_columns()` and collations from the stored
`CREATE TABLE` (`duckdb_tables().sql`: `duckdb_columns()` and
`information_schema` drop them). A text column without its own COLLATE takes
the session's `default_collation` as read at analysis time: a session that
runs the query under another default compares differently (documented in the
adapter). An `ENUM('a', ...)` column type, from the catalog or written in a
`CREATE TABLE`, is an Enumerated Domain (`ir/convert.py`
`_enum_type_values`). A fetcher that cannot read collations must leave
`data_type` None. Tests: `tests/test_duckdb_hydration.py` (issue 30).

Also fixed: the identity-comparison check (issue 23 function FDs) rebuilt a
column's type from its name and crashed on a user-defined type (`s mood`).
