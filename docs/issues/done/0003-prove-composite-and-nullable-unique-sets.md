# 3. Prove composite and nullable Unique Sets

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/3

## Parent

#1

## What to build

Extend the end-to-end analyzer to prove Unique Join Assertions from composite and nullable Unique Sets supplied either by Create Table declarations or explicit Knowledge. Cover the conservative equality semantics needed for complete key determination, constants, USING, and LEFT joins while preserving UNKNOWN for insufficient or unsupported predicates.

## Acceptance criteria

- [ ] Fully covering every member of a composite Unique Set in an INNER equality join returns PROVED.
- [ ] Covering only part of a composite Unique Set returns UNKNOWN and identifies the missing member as best-effort evidence.
- [ ] A right-side key member equated to a constant is treated as determined, allowing a composite Unique Set to be completed.
- [ ] A range or other non-determining right-side predicate does not cover a key member and returns UNKNOWN.
- [ ] Nullable Unique Sets prove at-most-one matching row for ordinary equality, while Candidate Keys additionally preserve their non-null meaning.
- [ ] Explicit Knowledge can provide relation columns, Unique Sets, and nullability without containing SQL definitions or parser structures.
- [ ] USING and LEFT joins participate in the same sound key-coverage reasoning as supported INNER ON joins.
- [ ] Null-safe equality and unsupported predicate forms return UNKNOWN rather than being approximated.
- [ ] Automated tests through `analyze` include happy paths for composite, nullable, constant-covered, USING, and LEFT cases and unhappy paths for partial, range, and null-safe cases.

## Blocked by

- #2

---

**Comment by polvoazul on 2026-08-27:**

Delivered by commit 1d97f74 and covered by the composite Unique Set and soundness tests. Current verification: 236 tests pass and ty check passes. Closing as complete.
