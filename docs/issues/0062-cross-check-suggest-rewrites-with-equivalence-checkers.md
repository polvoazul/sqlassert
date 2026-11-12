# 62. Cross-check `suggest` rewrites with SQL equivalence checkers

- **State:** open
- **Priority:** P3 (open): Provisional: not prioritized yet.
- **Theme:** suggestions
- **Labels:** check
- **Created:** 2026-11-11
- **Related:** [15: suggest](done/0015-suggest-provable-properties-for-unannotated-sql.md), [61: VeriEQL oracle](0061-symbolic-oracle-with-verieql.md)

## Why

Every Rewrite in `suggest` (INNER JOIN, redundant join, filter-
only join, GROUP BY as projection, `EXCEPT ALL`, …) claims the rewritten SQL
returns the same rows as a multiset. Today `tests/test_suggest_rewrites.py`
checks that on random DuckDB instances only. Several tools decide or bound
query equivalence under keys and NOT NULL, and can be run on our pairs:

| Tool | What | Code | Notes |
|---|---|---|---|
| **SQLSolver** (Ding et al., SIGMOD 2024) | Full proofs via LIA* → SMT; 346/359 Calcite + Spark rewrite pairs | [SJTU-IPADS/SQLSolver](https://github.com/SJTU-IPADS/SQLSolver), Java, Apache-2.0 | Answers EQ / NEQ / UNKNOWN / TIMEOUT |
| **VeriEQL** (OOPSLA 2024) | Bounded (≤ N rows), NULLs, integrity constraints, counterexamples | [VeriEQL/VeriEQL](https://github.com/VeriEQL/VeriEQL), Python | Shared with issue 61 |
| **QED** (Wang, Pan & Cheung, PVLDB 2024) | Q-expressions, full NULL semantics and constraints, SMT | no public repo found; ask the authors | |
| **Cosette / UDP** (Chu et al., CIDR 2017, PVLDB 2018) | U-semiring proofs, keys and FKs | [uwdb/Cosette](https://github.com/uwdb/Cosette), BSD-2, last push 2024 | Weak on NULLs; likely the least useful |

## What to do

1. Emit every Rewrite in the suite as a `(DDL, original, rewritten)`
   triple (a pytest hook, like the oracle's recording).
2. Run SQLSolver and VeriEQL on each triple; QED if the authors share it.
   Translate DDL constraints to each tool's input format; record what each
   tool cannot parse.
3. NEQ from any tool: a soundness bug, reduced to a regression. EQ from
   SQLSolver: the rewrite is proved independently of sqlassert. UNKNOWN /
   unsupported: listed.
4. Report per Rewrite kind: proved by which tool, bounded only, or
   unsupported. This is evidence for a paper (rewrites justified by property
   proofs, cross-checked by verifiers).

## Acceptance criteria

- [ ] Triple export for every Rewrite kind in the suite.
- [ ] SQLSolver and VeriEQL runs scripted (opt-in, outside the default
      suite), tools in gitignored `vendor/`.
- [ ] Results table per Rewrite kind and tool.
- [ ] Every NEQ fixed or filed.
