# 63. Generate random programs for the soundness oracle

- **State:** open
- **Labels:** check, needs-design
- **Created:** 2026-11-11
- **Related:** [11: SQL corpus](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md), [27: soundness oracle](0027-soundness-oracle-against-duckdb.md) (Part 2 consumes these programs), [61: VeriEQL oracle](0061-symbolic-oracle-with-verieql.md)

## Why

The oracle only judges programs someone wrote: our tests and, later, the
issue-11 corpus. Random program generation finds the combinations nobody
writes. Two tools to borrow from:

- **SQLancer** (Rigger & Su), [sqlancer/sqlancer](https://github.com/sqlancer/sqlancer),
  Java, MIT, active. Generates random schemas, data and queries per DBMS
  (DuckDB included) and judges them with result-level oracles: PQS (pivot
  row must be returned), NoREC (optimized vs unoptimizable rewrite), TLP
  (`Q = Q WHERE p ∪ Q WHERE NOT p ∪ Q WHERE p IS NULL`). 400+ bugs found in
  real DBMSs.
- **ParSEval** (PVLDB 18, 2025), [sfu-db/ParSEval](https://github.com/sfu-db/ParSEval),
  Python, Apache-2.0, takes DDL plus a query. Generates minimal instances that
  exercise every branch of the query's logical plan (branch coverage,
  symbolic reasoning, Z3), aimed at refuting equivalence. A better data
  generator than random values for joins and filters.

Our oracle is property-level: sqlassert claims a key, FD or non-null column,
and the instance checks that claim directly, which names the faulty rule.

## What to do

1. Program generator (Hypothesis strategies, or SQLancer's DuckDB generator
   driven from its CLI): random DDL with keys, UNIQUE, NOT NULL, FKs; queries
   over joins (all kinds), filters, GROUP BY, DISTINCT, set operations,
   windows, subqueries, CTEs, within what the IR lowers.
2. Feed each program to 27 Part 2's candidate generator (no markers) and
   judge PROVED candidates with the oracle. Shrink counterexample programs
   (Hypothesis) to a minimal `.sqltest` regression.
3. Try ParSEval's instance generation as an alternative to the oracle's
   random instances; compare counterexample-finding rate on known bugs
   (revert one of the Calcite-audit fixes and measure time to find it).
4. Opt-in, time-boxed run (`pytest --fuzz-programs --minutes N`).

## Acceptance criteria

- [ ] Generator covering the lowered IR's operators, documented.
- [ ] Opt-in fuzz run that reports PROVED-vs-counterexample with a shrunk
      program.
- [ ] Bug-reinsertion experiment: random instances vs ParSEval.
- [ ] Every real finding fixed with a regression.
