# 32. Infer simple CASE nullability

- **State:** done
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

- [x] `ASSERT NOT NULL` proves on such a CASE output and stays UNKNOWN
  otherwise (a NULL `THEN`, a missing `ELSE` without exhaustiveness, a
  nullable operand without `ELSE`).
- [x] Explanations name the arm that may be NULL.
- [x] Oracle judges the new proofs.

## Done

`rules/nullability.py` `_case_sources`: a simple CASE's null sources are its
THEN arms' and, unless `_else_unreachable` (exhaustive over a non-null
operand) proves no row reaches it, its ELSE's (a missing ELSE is NULL). Each
is labelled: "THEN arm 2 may be NULL", "ELSE may be NULL", "With no ELSE, a
row no WHEN matches is NULL". Tests: `tests/test_analyze_not_null.py`
(Simple CASE); the oracle judges the PROVED ones.
