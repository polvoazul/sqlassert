# 16. Assert and declare string `ENUM` domains

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

## What to build

Authors can constrain a column's non-null values to a finite
set of strings. Enumerated Domains can come from assertions, declarations, or
external Knowledge and can prove compatible downstream domain assertions.

## Design note: domains cross equalities

A domain crosses an equality: with `WHERE a = b` (or an inner-join
`ON a = b`), `b`'s domain is `a`'s, intersected with its own. Read this from
equality groups ([issue 26](0026-equality-groups.md)).

## Blocked by

- Task 2 (done) — Declare trusted Unique Sets.
- [issue 26](0026-equality-groups.md) — Equality groups.

## Acceptance criteria

- [ ] `ASSERT ENUM` proves that every possible non-null value belongs to the
  asserted string set.
- [ ] `DECLARE ENUM` and external Knowledge introduce trusted Enumerated Domain
  facts without claiming that every listed value occurs.
- [ ] A known smaller domain proves an assertion containing a superset, while a
  known domain containing uncovered possibilities leaves the assertion
  `UNKNOWN`.
- [ ] Enumerated Domains propagate through pass-through columns, renaming,
  filters, aliases, CTEs, and views.
- [ ] Null remains outside the Enumerated Domain and is governed independently
  by `NOT NULL`.
- [ ] Unknown reports identify possible values that the assertion does not
  cover.
- [ ] The domain glossary defines Enumerated Domain and distinguishes it from
  occurrence, nullability, and a database enum type.
