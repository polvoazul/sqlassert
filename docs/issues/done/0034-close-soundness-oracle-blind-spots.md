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

The oracle's skip count is 0 (697 programs, 480 PROVED assertions judged).
The two `per_user` self-joins got aliases; the self-referencing view test now
creates `plain_tree`. The oracle creates every schema a qualified table names,
runs `ALTER TABLE ... ADD UNIQUE` (as a unique index) after the other DDL so
`SET NOT NULL` on the same column works, and prefixes a checked Select's own
CTEs to its violation query, so a Foreign Key onto a CTE is judged by its
`NOT EXISTS` coverage query.
