# 21. Assert simple `EXACTLY ONE` joins

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

## What to build

Authors can prove that every left row of a simple
single-column equality join matches exactly one row on the right. The result
distinguishes uniqueness from guaranteed matching when proof is incomplete.

## Blocked by

Task 3 (done) — Assert and declare `NOT NULL`; [#19](0019-assert-and-declare-direct-foreign-keys.md) (Task 7) — Assert and
declare direct Foreign Keys.

## Acceptance criteria

- [ ] `ASSERT EXACTLY ONE` attaches to inner and left joins with a direct
  single-column equality predicate.
- [ ] The at-most-one half is proved by coverage of a right-side Unique Set.
- [ ] The at-least-one half is proved by a non-null left column with a Foreign
  Key to the exact right relation and predicate column.
- [ ] A proved inner join neither multiplies nor removes left rows; a proved
  left join also guarantees that no left row is null-extended for lack of a
  match.
- [ ] An unknown result reports RHS uniqueness, guaranteed matching, and
  non-nullness as separate missing evidence.
- [ ] Proven joins preserve established left-input properties on corresponding
  output columns.
