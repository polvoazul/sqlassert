# 68. Name suggestion types by what they mean to the user

- **State:** not planned
- **Priority:** P4 (not planned): Naming only: a hazard reads softer than it is.
- **Theme:** suggestions
- **Labels:** naming, ux
- **Created:** 2026-11-11
- **Related:** [15: suggest](done/0015-suggest-provable-properties-for-unannotated-sql.md)

## Why

`suggest` reports three types: rewrite, marker and hazard (`SuggestType`).
The command's name and "suggestion" read as optional advice, softer than a
hazard. In practice a hazard is the strongest of the three: SQL whose result
is probably not what its author meant (a fan trap, an arbitrary LIMIT, a
query that never returns a row, a grouping key not selected). A rewrite is
a simplification, a marker a fact worth writing down. Someone skimming the
output can take the order of severity backwards.

## What to do

Decide names (for the command, the types and the CLI's labels) that put
hazards first and read as "probably a bug", then rewrites ("simpler, same
result"), then markers ("provable, worth stating"). Consider ordering the
output by type and a summary line that leads with hazards.

## Acceptance criteria

- [ ] Names chosen and recorded in CONTEXT.md (with the terms to avoid).
- [ ] CLI, Python API and dbt plugin output renamed together, no aliases.
