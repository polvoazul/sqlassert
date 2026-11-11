# 65. Triage SQLCheck's anti-pattern catalog for `suggest` hazards

- **State:** open
- **Labels:** research
- **Created:** 2026-11-11
- **Related:** [15: suggest](done/0015-suggest-provable-properties-for-unannotated-sql.md)

## Why

SQLCheck (Dintyala, Narechania & Arulraj, SIGMOD 2020,
[arXiv 2004.10232](https://arxiv.org/abs/2004.10232)) detects SQL
anti-patterns (from Karwin's *SQL Antipatterns* catalog) by query and data
analysis, ranks them by impact and suggests fixes; it studied 1406
open-source applications. Its detections are heuristic and partly
data-derived; sqlassert's hazards are proved from structure. It is the
closest related work for `suggest`, and its catalog lists hazards we may lack.

## What to do

1. List SQLCheck's anti-patterns (paper and tool).
2. For each, record *covered* (our hazard or Rewrite), *add* (provable
   from SQL and schema: name the Property), *data-only* (needs data, so out
   of scope per ADR 0005), or *n/a* (schema design, physical, style).
3. For *covered* ones, note whether ours is a proof where theirs is a
   heuristic (fewer false positives): material for the related-work section.
4. File *add* rows as new issues.

## Acceptance criteria

- [ ] Triage table in this issue.
- [ ] *Add* rows filed.
