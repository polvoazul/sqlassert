# References

Literature behind sqlassert's properties: dependency theory (keys, FDs,
inclusion dependencies), SQL NULL semantics, and propagating dependencies
through query operators. Local copies (gitignored) live in `papers/`.

## External implementation sources

- **Apache Calcite** — metadata inference for functional dependencies, unique
  keys, predicates and null rejection. Use its implementation and tests as an
  audit source alongside the papers. [Source inventory and corpus notes](calcite-notes.md);
  [audit work](issues/0040-audit-inference-against-calcite.md).
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
