# Issues

Open work, one file per issue. Finished issues move to [`done/`](done/) instead of being deleted. Issues 11 and 14 were migrated from GitHub issues (polvoazul/sqlassert) on 2026-11-01; 16–25 from `TODO.md` and the README Ideas list. 26 and 27 were split out of 16 and 11.

- [11. Fuzz-test analyze against a real-world SQL corpus](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — open
- [15. Suggest provable properties for unannotated SQL](0015-suggest-provable-properties-for-unannotated-sql.md) — open
- [23. Close IR lowering gaps](0023-close-ir-lowering-gaps.md) — open (parent epic of 37, 38, 39)
- [27. Soundness oracle against DuckDB](0027-soundness-oracle-against-duckdb.md) — open (Part 1 done: `pytest --soundness-oracle`; Part 2 open)
- [37. Close the Paulley audit's missing rules](0037-close-the-paulley-audit-missing-rules.md) — open (child of 23)
- [39. Smaller lowering gaps](0039-smaller-lowering-gaps.md) — open (child of 23)
- [42. Lowering support v2](0042-lowering-support-v2.md) — not planned (P4 epic, split from 23)
- [49. Collation-aware Properties](0049-collation-aware-properties.md) — not planned (P4, child of 42: Properties under a collation; today any COLLATE is not identity)
- [45. Close the Calcite audit's missing proofs](0045-close-the-calcite-audit-missing-proofs.md) — open (from 40)
- [60. Audit inference against CockroachDB's FD library](0060-audit-inference-against-cockroachdb.md) — open (like 40; same lax FD definition as ours, `fd:` test corpus; TiDB optional)
- [61. Symbolic soundness oracle with VeriEQL](0061-symbolic-oracle-with-verieql.md) — open (needs design; every Property as a bounded equivalence; replaces 27's z3 alternative)
- [62. Cross-check `suggest` rewrites with SQL equivalence checkers](0062-cross-check-suggest-rewrites-with-equivalence-checkers.md) — open (SQLSolver, VeriEQL, QED, Cosette on every Rewrite)
- [63. Generate random programs for the soundness oracle](0063-generate-random-programs-for-the-oracle.md) — open (needs design; SQLancer-style generator, ParSEval instances; feeds 27 Part 2)
- [64. Triage WeTune's discovered rewrite rules](0064-triage-wetune-rewrite-rules.md) — open (research; like 15's Kossmann triage)
- [65. Triage SQLCheck's anti-pattern catalog](0065-triage-sqlcheck-anti-patterns.md) — open (research; hazards for 15)
- [66. Check our SQL semantics against Guagliardo & Libkin](0066-check-semantics-against-guagliardo-libkin.md) — open (research; NULL/bag rules, certifier probes for 46)
- [67. Recheck the Calcite audit against `FuncDepSet` (CALCITE-7205)](0067-recheck-calcite-funcdepset.md) — waiting on upstream
- [43. External integrations](0043-external-integrations.md) — open (epic, parent of 44, 51-54; backlog of CLI, editor, framework, data quality and agent integrations)
- [51. Validate Snowflake against a real engine](0051-validate-snowflake-against-a-real-engine.md) — not planned (P4, child of 43)
- [52. Validate BigQuery against a real engine](0052-validate-bigquery-against-a-real-engine.md) — not planned (P4, child of 43)
- [53. Validate Databricks against a real engine](0053-validate-databricks-against-a-real-engine.md) — not planned (P4, child of 43)
- [54. Validate Redshift against a real engine](0054-validate-redshift-against-a-real-engine.md) — not planned (P4, child of 43)
- [56. dbt: read the live warehouse catalog in the plugin's commands](0056-dbt-live-connection.md) — open (needs a decision: how to get the connection)
- [57. dbt: incremental models](0057-dbt-incremental-models.md) — open (bare minimum today; ideas deferred)
- [58. dbt: test and decide how findings map back to model files](0058-dbt-source-mapping.md) — open (deferred)
- [59. User-defined functions and table functions (Postgres, Spark, Hive)](0059-postgres-user-defined-functions.md) — not planned (P4: a SETOF UDF or a UDTF in a select list repeats rows; read CREATE FUNCTION and the catalog)
- [68. Name suggestion types by what they mean to the user](0068-name-suggestion-types-by-severity.md) — not planned (P4: a hazard reads softer than it is)
- [46. Certify dialect facts against real engines](0046-certify-dialect-facts.md) — open (probes, runner and provenance done; DuckDB, SQLite, Postgres, MySQL, ClickHouse certified; Spark left)

## Order

Done: 14, 16, 17, 18, 19, 20, 21, 22, 24, 26, 28, 29, 27 Part 1, 30, 31, 32, 33, 34, 35, 36, 38, 40, 41, 44, 47, 48, 50, 55, 15's markers and rewrites. Closed: 25 (its targets were deleted by 24; the oracle covers soundness). Run
`pytest --soundness-oracle` before merging any rules change.

Priorities (reasoning per item in `tmp/issue_tracker.html`):

- **P1:** none open. #41 and #38 are done.
- **P2:** #37 (Paulley audit's missing rules); #45 (Calcite audit's missing
  proofs; its lowering items, HAVING and USING first, lose proofs on common
  SQL); #15's remaining hazards and rewrites.
- **P3, needs downloaded corpora:** #11, #27 Part 2.
- **P3:** #39 (smaller lowering gaps).
- **Integrations:** #44 (v1: `check` CLI, dialects, dbt) is done; the rest of
  #43 is backlog.
- **P4, not planned:** #42 (lowering support v2): window functions (and
  #33's CTE part, which needs them), recursive CTEs, column lineage, LATERAL
  and UNNEST, ADD COLUMN, scalar subqueries in grouped select lists and in ON.
- **P4, not planned:** #59 (UDFs and UDTFs in Postgres, Spark and Hive: a
  SETOF function or a UDTF in a select list repeats rows; IMMUTABLE and STRICT
  facts; from CREATE FUNCTION and the catalog).
- **P4, not planned:** #68 (naming: `suggest` and "hazard" read softer than
  a hazard is; rename the types by severity).
- **P4, not planned:** #51-54 (validate Snowflake, BigQuery, Databricks and
  Redshift against a real engine; needs warehouse credentials).

## Done

- [1. Spec: Rewrite the property engine around a relational IR and Clingo](done/0001-spec-rewrite-the-property-engine-around-a-relational-ir-and.md)
- [2. Prove one declared Unique Join through Clingo](done/0002-prove-one-declared-unique-join-through-clingo.md)
- [3. Prove composite and nullable Unique Sets](done/0003-prove-composite-and-nullable-unique-sets.md)
- [4. Propagate uniqueness through relation instances, filters, and projections](done/0004-propagate-uniqueness-through-relation-instances-filters-and.md)
- [5. Analyze CTE and FROM-subquery plans](done/0005-analyze-cte-and-from-subquery-plans.md)
- [6. Expand Create Views with provenance and cycle detection](done/0006-expand-create-views-with-provenance-and-cycle-detection.md)
- [7. Derive uniqueness from Aggregate and Distinct](done/0007-derive-uniqueness-from-aggregate-and-distinct.md)
- [8. Derive uniqueness from QualifyByPartition](done/0008-derive-uniqueness-from-qualifybypartition.md)
- [9. Return conservative diagnostics for the complete MVP boundary](done/0009-return-conservative-diagnostics-for-the-complete-mvp-boundar.md)
- [10. Cut over to analyze and retire the legacy analyzer](done/0010-cut-over-to-analyze-and-retire-the-legacy-analyzer.md)
- [12. Add Unique Set Assertions for Select Expressions (UNIQUE/PRIMARY KEY markers)](done/0012-add-unique-set-assertions-for-select-expressions-unique-prim.md)
- [13. Refactoring](done/0013-refactoring.md)
- [14. Hydrate analysis from database catalogs with recursive view expansion](done/0014-hydrate-analysis-from-database-catalogs-with-recursive-view.md)
- [16. Assert and declare string `ENUM` domains](done/0016-assert-and-declare-string-enum-domains.md)
- [17. Complete typed `ENUM` support](done/0017-complete-typed-enum-support.md)
- [18. Assert exhaustive simple `CASE` expressions](done/0018-assert-exhaustive-simple-case-expressions.md)
- [19. Assert and declare direct Foreign Keys](done/0019-assert-and-declare-direct-foreign-keys.md)
- [20. Propagate Foreign Keys through queries](done/0020-propagate-foreign-keys-through-queries.md)
- [21. Assert simple `EXACTLY ONE` joins](done/0021-assert-simple-exactly-one-joins.md)
- [22. Complete `EXACTLY ONE` reasoning](done/0022-complete-exactly-one-reasoning.md)
- [24. Functional-dependency reasoning](done/0024-functional-dependency-reasoning.md)
- [25. Check rule consistency invariants](done/0025-check-rule-consistency-invariants.md)
- [26. Equality groups](done/0026-equality-groups.md)
- [28. Analyze join markers inside CTE bodies](done/0028-analyze-join-markers-inside-cte-bodies.md)
- [29. ENUM: distinct string literals may compare equal](done/0029-enum-string-literals-may-coincide.md)
- [30. Read enum types and collations from the catalog](done/0030-read-enum-types-and-collations-from-the-catalog.md)
- [31. Enumerated Domains through set operations](done/0031-enumerated-domains-through-set-operations.md)
- [32. Infer simple CASE nullability](done/0032-infer-simple-case-nullability.md)
- [33. INNER JOIN suggestion across join chains and CTEs](done/0033-inner-join-suggestion-across-join-chains-and-ctes.md)
- [34. Close the soundness oracle's blind spots](done/0034-close-soundness-oracle-blind-spots.md)
- [35. Audit FD propagation against Paulley](done/0035-audit-fd-propagation-against-paulley.md)
- [38. Lower ORDER BY and scalar subqueries](done/0038-lower-order-by-and-select-list-subqueries.md)
- [41. Join cardinality markers and the fan-trap hazard](done/0041-join-cardinality-markers-and-fan-trap-smell.md)
- [44. v1 external integrations](done/0044-v1-external-integrations.md)
- [47. Discovery: one base fetcher, a subclass per dialect](done/0047-discovery-base-class.md)
- [48. dbt plugin as its own package](done/0048-dbt-plugin-package.md)
- [50. dbt: read `catalog.json`](done/0050-dbt-catalog-json.md)
- [55. Run `sqlassert-dbt suggest` on dbt model files](done/0055-dbt-suggest-model-files.md)
