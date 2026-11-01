# 30. Read enum types and collations from the catalog

- **State:** open
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

- [ ] A catalog column of an enum type yields `EnumeratedDomain` Knowledge with
  its members as string values.
- [ ] A catalog `VARCHAR` column with no collation is marked `binary_text`; one
  with a collation is not. If DuckDB cannot report collation reliably, document
  that and keep the conservative default.
- [ ] Tests against an in-memory DuckDB connection.
