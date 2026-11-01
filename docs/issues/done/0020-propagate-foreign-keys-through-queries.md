# 20. Propagate Foreign Keys through queries

- **State:** done
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

## What to build

Established Foreign Keys survive transformations that cannot
invalidate referential coverage, and inner joins can establish relationships
for output columns structurally restricted to matching target rows.

## Blocked by

 [#19](0019-assert-and-declare-direct-foreign-keys.md) (Task 7) — Assert and declare direct Foreign Keys; #26 (done; see [functional-dependencies.md](../../functional-dependencies.md#equality-groups)) — Equality groups (an inner equality join establishes a Foreign Key only through value equality; `_equal_columns` gives it).

## Acceptance criteria

- [ ] Foreign Keys propagate through direct projections, renaming, filters of
  the referencing relation, aliases, CTEs, and views.
- [ ] Composite column order and target identity remain intact during
  propagation.
- [ ] An inner equality join can establish a Foreign Key for corresponding
  output columns when the target columns form a Unique Set.
- [ ] Filtering or otherwise narrowing the referenced relation does not inherit
  a Foreign Key to its broader source unless coverage is independently proved.
- [ ] Unsupported expressions or transformations conservatively stop
  propagation instead of guessing.
- [ ] Assertions over derived relations report the closest missing relationship
  evidence.
