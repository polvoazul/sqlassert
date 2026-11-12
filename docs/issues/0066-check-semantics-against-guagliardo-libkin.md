# 66. Check our SQL semantics against Guagliardo & Libkin

- **State:** open
- **Priority:** P3 (research): Provisional: not prioritized yet.
- **Theme:** soundness
- **Labels:** research, check
- **Created:** 2026-11-11
- **Related:** [46: certify dialect facts](0046-certify-dialect-facts.md)

## Why

- **Guagliardo & Libkin, *A Formal Semantics of SQL Queries, Its Validation,
  and Applications*** (PVLDB 11(1), 2017,
  [pdf](https://www.pure.ed.ac.uk/ws/files/46755003/Guagliardo_Libkin_2017_A_Formal_Semantics.pdf)).
  Formal semantics of SELECT-FROM-WHERE with subqueries, bags, set
  operations and NULLs; validated on random queries against PostgreSQL and
  Oracle; proves basic SQL equals relational algebra under bags and NULLs.
- **Guagliardo & Libkin, *How "Standard" is the SQL Standard?*** (AMW 2018,
  [pdf](https://www.ceur-ws.org/Vol-2100/paper16.pdf)). Engines claiming the
  standard diverge even on simple statements; there is no reference
  implementation or test suite.

Our rules encode NULL and bag semantics implicitly (null rejection, padding,
three-valued WHERE, set-operation keys). The first paper is a precise
reference to check them against; the second is the framing for issue 46
(per-dialect certified facts instead of one standard).

## What to do

1. Read the semantics' rules for WHERE (3VL), subqueries (`IN`, `EXISTS`,
   `ANY`/`ALL` with NULLs), set operations and duplicates. For each, name the
   sqlassert rule that depends on it and check that they agree; disagreements
   become `.sqltest` regressions checked by the oracle.
2. Compare their validation (random queries vs real engines) with our
   certifier (`scripts/certify_dialects.py`): what they test that we do not.
3. Use "How Standard" to list divergences relevant to our Properties that
   issue 46's probes do not yet cover; add probes.

## Acceptance criteria

- [ ] Rule-by-rule comparison notes (in `docs/literature-notes.md`).
- [ ] Disagreements reduced to regressions or filed.
- [ ] New certifier probes for uncovered divergences, or a note that none
      apply.
