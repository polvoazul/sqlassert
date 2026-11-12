# 71. Rewrites carry exact edits

- **State:** open
- **Priority:** P2 (open): a Rewrite's prose and the tests' hand-written rewritten SQL can drift apart.
- **Theme:** suggestions
- **Labels:** suggest, testing
- **Created:** 2026-11-12
- **Related:** [62: cross-check rewrites](0062-cross-check-suggest-rewrites-with-equivalence-checkers.md) (split from it), [15: suggest](done/0015-suggest-provable-properties-for-unannotated-sql.md)

## Why

A Rewrite says what to change in prose ("write JOIN", "drop GROUP BY and
write SELECT ..."), and `tests/test_suggest_rewrites.py` compares the
original against a rewritten query written by hand. Nothing ties the two
together: the message, the hand-written query and what a user would type can
all differ. Issue 62's triple export also needs the exact rewritten SQL.

## What to do

1. Each Rewrite carries text edits (start, end, replacement) on the original
   SQL, placed from SQLGlot's token positions (the AST does not keep every
   clause's span). An edit never deletes a comment or a Property Marker
   inside the span it replaces.
2. Everything shown is derived from the edits: the CLI prints the reason and
   a unified diff; the Python API exposes the edits and the edited SQL; the
   dbt report shows the diff against the compiled SQL. A Rewrite with Caveat
   still shows its caveat.
3. The equivalence tests take the Rewrite `suggest` offers, apply its edits,
   and compare on random instances, with the caveat `typeof` checks where
   they exist. The guard tests stay. One hand-written expected diff per
   RewriteKind stays as independent truth.

## Acceptance

- [ ] Every RewriteKind has exact edits, or is listed here with why not.
- [ ] No hand-written rewritten SQL in `EQUIVALENT`.
- [ ] CLI, Python API and dbt report show the edits.
