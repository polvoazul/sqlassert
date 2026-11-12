# 61. Symbolic soundness oracle with VeriEQL

- **State:** open
- **Priority:** P3 (needs design): Provisional: not prioritized yet.
- **Theme:** soundness
- **Labels:** check, needs-design
- **Created:** 2026-11-11
- **Related:** [27: soundness oracle](0027-soundness-oracle-against-duckdb.md) (replaces its "Deferred alternative — symbolic oracle with z3"), [62: cross-check rewrites](0062-cross-check-suggest-rewrites-with-equivalence-checkers.md)

## Why

The DuckDB oracle (27) searches random tiny instances: a counterexample is
certain, a pass is only "probably". Issue 27 deferred writing our own z3
encoding because it is a second model of SQL to write and trust. VeriEQL
already is that model, and it is a tool, not just a paper:

- **VeriEQL** (He, Zhao, Wang & Wang, OOPSLA 2024, distinguished paper),
  [github.com/VeriEQL/VeriEQL](https://github.com/VeriEQL/VeriEQL), Python,
  custom license (read `license.md` before vendoring). Bounded equivalence:
  symbolic tables up to N rows, NULLs and integrity constraints (keys, FKs,
  NOT NULL) modelled; returns a counterexample database on failure. Found
  bugs in MySQL and Calcite.

## What to do

Every Property becomes an equivalence of two queries over the same DDL, so a
bounded equivalence checker decides it:

| Property on `Q` | `Q1` | `Q2` |
|---|---|---|
| Not null `c` | `SELECT * FROM Q` | `SELECT * FROM Q WHERE c IS NOT NULL` |
| Unique Set `K` | `SELECT K FROM Q WHERE` every K column `IS NOT NULL` | same with `SELECT DISTINCT K` |
| Non-Null Unique Set `K` | both of the above | |
| Unique / Many-to-one join | `SELECT lk FROM l JOIN r ON p` | `SELECT DISTINCT …` (left key `lk`) |
| `DETERMINES(X -> Y)` | `SELECT X FROM (SELECT DISTINCT X, Y FROM Q WHERE` X non-null`)` | same with `SELECT DISTINCT X` (DISTINCT counts NULL = NULL in Y, as our lax FD does) |

1. Install VeriEQL in a gitignored `vendor/`, run it on a few hand-written
   pairs, record its supported SQL subset and the bound N it handles in
   seconds.
2. Write the property-to-equivalence translation for the table above
   (Root Select first; views/CTEs by extracting the node's SQL as issue 27
   does).
3. Run it as a second judge in `pytest --soundness-oracle --symbolic`
   (opt-in): for each PROVED assertion, EQ within bound N is a bounded proof,
   NEQ is a soundness bug with VeriEQL's counterexample turned into a
   regression. Record per program which oracle judged it.
4. For SPJ queries, N = 2 is complete (Paulley Thm 11/12): report those
   assertions as **verified**, not just tested.
5. Optional: run it on UNKNOWN outcomes to sort missed proofs (EQ within N)
   from correct UNKNOWNs (NEQ), the completeness metric of 27 Part 2.

## Acceptance criteria

- [ ] VeriEQL's supported subset and runtime per bound are documented.
- [ ] Property-to-equivalence translation for every Property in the table,
      each checked on a PROVED and an UNKNOWN example.
- [ ] Opt-in symbolic oracle run over the existing suite; counts of
      verified / bounded-EQ / NEQ / unsupported.
- [ ] Every NEQ is fixed or filed; issue 27's z3 section points here.
