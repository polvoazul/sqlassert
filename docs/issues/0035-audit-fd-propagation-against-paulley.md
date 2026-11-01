# 35. Check: audit FD propagation against Paulley, operator by operator

- **State:** open
- **Labels:** check, ready-for-agent
- **Created:** 2026-11-01

A check, not a feature: compare our rules with the thesis they came from and
turn every gap into a test or an issue.

## Why

[`functional-dependencies.md`](../functional-dependencies.md) and issue 24 were
built from Paulley 2000 (`papers/2000-paulley-...pdf`), but nobody has walked
the thesis rule by rule against `sqlassert/rules/`. Known gaps (set
operations, windows, subqueries, scalar-function FDs) are scattered across
issues 23 and 31; unknown ones are not tracked anywhere.

## What to do

1. Read Paulley chapters 2–3 (FD and key derivation per operator: projection,
   restriction, cartesian product, inner/left/full outer join, grouping and
   aggregation, distinct, set operations, scalar functions, subqueries, null
   handling, strict vs lax).
2. Produce a table in `docs/functional-dependencies.md` (or a section of it):
   one row per Paulley rule, with page, our implementing code
   (file/function), and status: **implemented**, **intentionally different**
   (say why), or **missing**.
3. For every *implemented* row without a test, add one (PROVED case and, where
   Paulley gives one, a counterexample that must stay UNKNOWN).
4. For every *missing* row, file an issue or add it to an existing one (23, 31).
5. Resolve the two suspected text-extraction errors listed at the end of
   [`literature-notes.md`](../literature-notes.md) (Thm 3 lax-equivalence
   bullet 3; the definiteness condition in Dependency-closure) against the PDF,
   and fix our docs or rules if they relied on the wrong reading.

## Acceptance criteria

- [ ] Audit table covers every operator rule in Paulley chapters 2–3.
- [ ] Every implemented rule has a test; oracle 0 counterexamples.
- [ ] Every missing rule is tracked in an issue.
- [ ] The two extraction errors are resolved.
