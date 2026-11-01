# 32. Infer simple CASE nullability

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01

## Problem

`ir.SimpleCase` (issue 18) gives its output an Enumerated Domain, but
nullability treats it as possibly NULL.

## Rule

A simple CASE output is non-null when every `THEN` value is non-null and
either its `ELSE` is non-null, or it has no `ELSE` but is proved exhaustive
over a non-null operand.

## Acceptance criteria

- [ ] `ASSERT NOT NULL` proves on such a CASE output and stays UNKNOWN
  otherwise (a NULL `THEN`, a missing `ELSE` without exhaustiveness, a
  nullable operand without `ELSE`).
- [ ] Explanations name the arm that may be NULL.
- [ ] Oracle judges the new proofs.
