# Issues

Open work, one file per issue. Finished issues move to [`done/`](done/) instead of being deleted. Issues 11 and 14 were migrated from GitHub issues (polvoazul/sqlassert) on 2026-11-01; 16–25 from `TODO.md` and the README Ideas list. 26 and 27 were split out of 16 and 11.

- [11. Fuzz-test analyze against a real-world SQL corpus](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — open
- [15. Suggest provable properties for unannotated SQL](0015-suggest-provable-properties-for-unannotated-sql.md) — open
- [23. Close IR lowering gaps](0023-close-ir-lowering-gaps.md) — open (epic)
- [27. Soundness oracle against DuckDB](0027-soundness-oracle-against-duckdb.md) — open (Part 1 done: `pytest --soundness-oracle`; Part 2 open)
- [30. Read enum types and collations from the catalog](0030-read-enum-types-and-collations-from-the-catalog.md) — open
- [31. Enumerated Domains through set operations](0031-enumerated-domains-through-set-operations.md) — open
- [32. Infer simple CASE nullability](0032-infer-simple-case-nullability.md) — open
- [33. INNER JOIN suggestion across join chains and CTEs](0033-inner-join-suggestion-across-join-chains-and-ctes.md) — open
- [34. Close the soundness oracle's blind spots](0034-close-soundness-oracle-blind-spots.md) — open
- [35. Check: audit FD propagation against Paulley, operator by operator](0035-audit-fd-propagation-against-paulley.md) — open (check)

## Order

Done: 14, 16, 17, 18, 19, 20, 21, 22, 24, 26, 28, 29, 27 Part 1, 15 Mid and High levels. Closed: 25 (its targets were deleted by 24; the oracle covers soundness). Run
`pytest --soundness-oracle` before merging any rules change.

Priorities (reasoning per item in the open-issues map artifact):

- **P1, now:** #34 oracle blind spots; #23 warn when a connection would help
  (ADR 0005); #23 outer and cross joins (Paulley §3.2.3, §3.2.10).
- **P2, next:** #23 set-op arms with #31; #23 function FDs (Paulley §3.1.4);
  #23 correlated subqueries (Paulley §4.4.2); #15 Kossmann triage, then Low
  smells; #15 annotate flag; #32; #30; #33 chain part.
- **P3, needs downloaded corpora:** #11, #27 Part 2.
- **P4, out of scope for now (not covered by Paulley):** #23 window functions
  (and #33's CTE part, which needs them), recursive CTEs, column lineage, semi
  and anti joins and LATERAL, CAST injectivity.

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
