# References

Literature behind sqlassert's properties: dependency theory (keys, FDs,
inclusion dependencies), SQL NULL semantics, and propagating dependencies
through query operators. Local copies (gitignored) live in `papers/`.

## External implementation sources

- **Apache Calcite** — metadata inference for functional dependencies, unique
  keys, predicates and null rejection. Use its implementation and tests as an
  audit source alongside the papers. [Source inventory and corpus notes](calcite-notes.md);
  [audit work](issues/done/0040-audit-inference-against-calcite.md).
- **Begoli, Camacho-Rodríguez, Hyde, Mior & Lemire — *Apache Calcite: A
  Foundational Framework for Optimized Query Processing Over Heterogeneous
  Data Sources*** (SIGMOD 2018). Architecture background, to read:
  [paper](https://arxiv.org/abs/1802.10233). Specific inference behavior must
  be checked against versioned source and tests.

## Read

### Lindner, Ritter & Naumann — *Enabling Data Dependency-based Query Optimization* (arXiv:2406.06886, 2024; rev. 2025)

`papers/2024-lindner-enabling-data-dependency-based-query-optimization.pdf`

Discovers UCCs (keys), FDs, ODs (order dependencies) and INDs (foreign keys)
from data statistics inside Hyrise and uses them for three optimizer rewrites:
O-1 dependent group-by reduction (`GROUP BY c_sk, c_name` → `GROUP BY c_sk`),
O-2 join → semi-join (S's join key unique and no S column used later), and O-3
join → predicate (key + IND guarantee exactly one partner). Reports 35% (TPC-DS)
and 29% (JOB) geometric-mean speedups.

Key insights for sqlassert:
- Their propagation table (§6.1, Table 1) matches our rules: GROUP BY/DISTINCT
  create keys; an equi-join on a unique key preserves the other side's keys;
  outer joins, theta joins and unions drop them; equi-join keys determine each
  other (`a ↔ x`).
- Their dependencies come from data, so they can hold by chance; ours are proved
  from structure. Their FDs only have a single key column on the left (§5.2), a
  limit of data validation cost, not of propagation.
- NULLs are essentially ignored (only `x IS NOT NULL` preserving an IND).
- The same facts that prove properties enable rewrites, so sqlassert could emit
  lint hints such as "redundant GROUP BY column" or "join only filters".

## To read

- **Paulley — *Exploiting Functional Dependence in Query Optimization*** (PhD
  thesis, University of Waterloo, 2000). FDs propagated through SQL operators,
  including NULLs and outer joins. Closest match to the engine's problem.
  `papers/2000-paulley-exploiting-functional-dependence-in-query-optimization.pdf`
- **Kossmann, Papenbrock & Naumann — *Data dependencies for query optimization: a
  survey*** (VLDB Journal 31(1), 2022). Catalogue of 60+ dependency-based
  optimizations by dependency type and operator; a map for future properties.
  `papers/2022-kossmann-data-dependencies-for-query-optimization-survey.pdf`
- **Liu et al. — *Leveraging Application Data Constraints to Optimize
  Database-Backed Web Applications*** (PVLDB 16(6), 2023). Statically infers
  undeclared constraints (INDs, nullability) from application code; relevant to
  sourcing Knowledge from outside the SQL.
  `papers/2023-liu-leveraging-application-data-constraints.pdf`
- **Maier — *The Theory of Relational Databases*** (1983). FDs, closure,
  Armstrong's axioms, covers.
- **Abiteboul, Hull & Vianu — *Foundations of Databases*** (1995, "Alice book").
  Dependency theory; incomplete information (possible worlds, certain answers).
- **Hartmann & Link — possible and certain keys** for SQL tables with NULLs.
- **Darwen — *The Role of Functional Dependence in Query Decomposition*** (in Date
  & Darwen, *Relational Database Writings 1989–1991*). FD inference through
  relational operators.
- **Beeri & Bernstein (1979)** — linear-time attribute closure.

### FD libraries in other optimizers

- **CockroachDB `FuncDepSet`** —
  [`pkg/sql/opt/props/func_dep.go`](https://github.com/cockroachdb/cockroach/blob/master/pkg/sql/opt/props/func_dep.go)
  (Go; CockroachDB Software License, so read, don't copy). Strict and lax FDs
  and keys, constants, equivalences, not-null columns; `ComputeClosure`,
  `ReduceCols`, `ProjectCols`, `MakeLeftOuter`, `MakeFullOuter`, `MakeApply`,
  `MakeMax1Row`, `Verify`. Its header cites Paulley and lists where it departs
  from him. Most relevant: **its lax FD is stricter than Paulley's** (his
  Def 2.19 variant). Paulley allows rows `(1, 1), (1, NULL)` under `a ~~> b`;
  CockroachDB forbids them, so a lax FD turns strict once only the determinant
  is non-null. Compare with our lax rules before implementing. It also uses
  no tuple identifier (ι). Audit corpus: `pkg/sql/opt/memo/testdata/logprops/*`
  prints an `fd:` line per plan node (260 in `join` alone), like Calcite's
  `RelMetadataTest`.
- **TiDB `FDSet`** —
  [`pkg/planner/funcdep`](https://pkg.go.dev/github.com/pingcap/tidb/pkg/planner/funcdep).
  Same strict/lax model; upgrades lax FDs to strict from predicates. Apache
  license, so code is reusable.
- **Calcite `FuncDepSet`** — being added under CALCITE-7205; recheck our
  [Calcite audit](calcite-audit.md) against it when it lands.

### Testing database logic (paper 1: soundness of derived properties)

- **Rigger & Su — SQLancer** ([github](https://github.com/sqlancer/sqlancer),
  MIT; 400+ bugs in widely used DBMSs). Oracles compare query *results*: PQS
  (OSDI 2020) picks a pivot row and builds a query that must return it; NoREC
  (ESEC/FSE 2020) compares an optimized query with an unoptimizable rewrite;
  TLP (OOPSLA 2020) checks `Q = Q WHERE p ∪ Q WHERE NOT p ∪ Q WHERE p IS
  NULL`. Later: Query Plan Guidance, CERT (cardinality estimates),
  Differential Query Plans, CODDTest. Our angle: an oracle on the derived
  *property* (key, FD, max-one-row), which names the faulty rule, and is
  complete for SPJ queries on 2-row instances (Paulley Thm 11/12).
- **ParSEval** (PVLDB 18, 2025) — plan-aware test-database generation to
  refute query equivalence. Prior art for our tiny-instance generator.

### SQL semantics across engines (paper 3: dialect certification)

- **Guagliardo & Libkin — *A Formal Semantics of SQL Queries, Its Validation,
  and Applications*** (PVLDB 11(1), 2017,
  [pdf](https://www.pure.ed.ac.uk/ws/files/46755003/Guagliardo_Libkin_2017_A_Formal_Semantics.pdf)).
  Semantics for SELECT-FROM-WHERE with subqueries, bags and NULLs, validated
  on random queries against PostgreSQL and Oracle. Proves basic SQL ≡
  relational algebra with bags and NULLs; shows 3-valued logic is not needed.
  One model; we certify facts per dialect and version.
- **Guagliardo & Libkin — *How "Standard" is the SQL Standard?*** (AMW 2018,
  [pdf](https://www.ceur-ws.org/Vol-2100/paper16.pdf)). Engines claiming the
  standard diverge even on simple statements; no reference implementation or
  test suite exists. The motivation section paper 3 needs.

### Query equivalence verifiers (whole-rewrite proofs; we prove one property)

- **Chu, Murphy, Roesch, Cheung & Suciu — UDP** (PVLDB 2018,
  [arXiv](https://arxiv.org/abs/1802.02229)). U-semiring semantics; queries
  become U-expressions, keys and FKs become identities; 68 rewrites verified.
  The Cosette line; Liu et al. use it. Weak on NULLs.
- **Wang, Pan & Cheung — QED** (PVLDB 17(11), 2024,
  [pdf](https://vldb.org/pvldb/vol17/p3602-wang.pdf)). Q-expressions in
  first-order logic with full NULL semantics and integrity constraints, via
  SMT. Compares with SPES and UDP.
- **Ding et al. — SQLSolver** (SIGMOD 2024,
  [github](https://github.com/SJTU-IPADS/SQLSolver)). Equivalence via LIA*
  (linear integer arithmetic with unbounded sums) reduced to SMT; proves
  346/359 Calcite and Spark rewrite pairs.
- **Wang et al. — WeTune** (SIGMOD 2022). Enumerates plan templates to
  discover rewrite rules, verifies them with SMT; 1106 rules. Rule
  constraints include keys and not-null: a list of assertions worth proving.
- **He, Zhao, Wang & Wang — VeriEQL** (OOPSLA 2024, distinguished paper,
  [arXiv](https://arxiv.org/abs/2403.03193)). Bounded equivalence over
  symbolic tuples (integers + uninterpreted functions), NULLs and rich
  integrity constraints; >95% of its benchmarks need constraints. Its
  counterexamples found bugs in MySQL and Calcite. Candidate for the deferred
  symbolic oracle in issue 11.

### SQL lint (suggest smells)

- **Dintyala, Narechania & Arulraj — SQLCheck** (SIGMOD 2020,
  [arXiv](https://arxiv.org/abs/2004.10232)). Detects anti-patterns by query
  plus data analysis, ranks them by impact, suggests rule-based fixes;
  studied 1406 open-source applications. Heuristic; our smells are proved.
