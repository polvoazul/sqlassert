# 31. Enumerated Domains through set operations

- **State:** open
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

- [ ] `SetOperation` records each arm's columns, positionally paired.
- [ ] Domains flow by the rules above, with tests and oracle coverage.
- [ ] `UNION BY NAME` stays unbounded (or pairs by name, if lowered).
