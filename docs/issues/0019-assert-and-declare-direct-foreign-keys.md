# 19. Assert and declare direct Foreign Keys

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

## What to build

Authors can obtain, assert, or declare a Foreign Key between
columns of a relation and a Unique Set of a named relation. Direct table
relationships support qualified names, composite keys, and SQL `MATCH SIMPLE`
null semantics.

## Blocked by

Task 2 (done) — Declare trusted Unique Sets.

## Acceptance criteria

- [ ] Public Knowledge can represent ordered referencing columns, a qualified
  referenced relation, and ordered referenced columns.
- [ ] Column-level and table-level Foreign Keys are collected from supported
  table declarations.
- [ ] `ASSERT FOREIGN KEY` and `DECLARE FOREIGN KEY` work for single and
  composite keys with positional column pairing.
- [ ] A Foreign Key requires both referential coverage and a proved or declared
  Unique Set on the referenced columns.
- [ ] Under `MATCH SIMPLE`, a row with any null referencing component is exempt;
  nullable referencing columns do not invalidate the Foreign Key itself.
- [ ] Unknown source columns, target relations, target columns, relationships,
  and target uniqueness produce precise diagnostics or explanations.
- [ ] A declaration produces no assertion result and is identified as trusted
  evidence in downstream proofs.
