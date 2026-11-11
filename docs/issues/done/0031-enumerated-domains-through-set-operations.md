# 31. Enumerated Domains through set operations

- **State:** done
- **Labels:** ready-for-agent
- **Created:** 2026-11-01

## Problem

A `UNION`, `INTERSECT` or `EXCEPT` output column gets no Enumerated Domain,
because the IR tracks only the left arm's columns
([issue 23](0023-close-ir-lowering-gaps.md), set-operation item).

## What to build

Pair arms by position (plain `UNION`, not `UNION BY NAME`):

- `UNION [ALL]`: the union of both arms' domains; unbounded if either is.
- `INTERSECT`: either arm's domain bounds the output.
- `EXCEPT`: the left arm's domain bounds the output.

Needs the right arm's columns in the IR, which also unblocks other facts
through set operations.

## Acceptance criteria

- [x] `SetOperation` records each arm's columns, positionally paired.
- [x] Domains flow by the rules above, with tests and oracle coverage.
- [x] `UNION BY NAME` stays unbounded (or pairs by name, if lowered).

## Done

`ir.SetOperation.arms` pairs inputs by position; `rules/domain.py`
`_set_operation_domain` applies the rules above. A UNION whose inputs give
values of different types is not enumerated (the output type may convert one).
An INTERSECT is bounded by its right input's string values only when the left
column compares strings by identity: the row carries the left value, which
under `NOCASE` may be `'P'` for a right `'p'`. `UNION BY NAME` stays
unbounded. Tests: `tests/test_analyze_enumerated_domain.py` (set operations).
