# 22. Complete `EXACTLY ONE` reasoning

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

## What to build

Exactly One Join Assertions work across composite keys,
aliases, and safely derived relations while conservatively rejecting predicates
or transformations that can invalidate guaranteed matching.

## Blocked by

 #20 (done; see [functional-dependencies.md](../functional-dependencies.md#referential-coverage-through-queries)) (Task 8) — Propagate Foreign Keys through queries; #21 (done) (Task 9) —
Assert simple `EXACTLY ONE` joins.

## Acceptance criteria

- [ ] Composite equality predicates prove both uniqueness and matching using
  positional Foreign Key and Unique Set coverage.
- [ ] Aliases, renamed pass-through columns, CTEs, views, and supported derived
  relations retain the identities needed for the proof.
- [ ] RHS filters and additional predicates make guaranteed matching unknown
  unless their safety is independently proved.
- [ ] A Foreign Key to a base relation is not treated as coverage of a narrowed
  version of that relation.
- [ ] Explanations identify every missing proof half for composite and derived
  joins.
- [ ] A proved assertion propagates the left input's Unique Sets, non-nullness,
  Enumerated Domains, and Foreign Keys to corresponding join outputs.
- [ ] Unsupported join kinds and predicates remain explicit `UNKNOWN` outcomes
  or diagnostics rather than optimistic proofs.
