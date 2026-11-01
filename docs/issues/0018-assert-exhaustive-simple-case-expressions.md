# 18. Assert exhaustive simple `CASE` expressions

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

## What to build

Authors can require a simple `CASE` expression to handle
every value in its operand's known Enumerated Domain explicitly. A proved case
also supplies the finite domain of its result when every result arm is a
supported literal.

## Blocked by

 [#17](0017-complete-typed-enum-support.md) (Task 5) — Complete typed `ENUM` support.

## Acceptance criteria

- [ ] An `ASSERT EXHAUSTIVE` marker attaches unambiguously to the immediately
  following simple `CASE` expression.
- [ ] The assertion is proved only when every member of the operand's known
  Enumerated Domain has an explicit matching `WHEN` arm.
- [ ] An `ELSE` arm does not substitute for explicit enum-member coverage.
- [ ] Null handling remains separate and depends on the operand's `NOT NULL`
  property when required by the caller.
- [ ] Missing domain members are reported clearly for an `UNKNOWN` outcome.
- [ ] A proved exhaustive case with supported literal results contributes an
  Enumerated Domain for its output expression.
- [ ] Searched cases, duplicate arms, unsupported `WHEN` values,
  `DECLARE EXHAUSTIVE`, and invalid attachment sites are diagnosed.
