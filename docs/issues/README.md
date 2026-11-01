# Issues

Open work, one file per issue. Issues 11 and 14 were migrated from GitHub issues (polvoazul/sqlassert) on 2026-11-01; 16–25 from `TODO.md` and the README Ideas list. 26 and 27 were split out of 16 and 11.

- [11. Fuzz-test analyze against a real-world SQL corpus](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — open
- [15. Suggest provable properties for unannotated SQL](0015-suggest-provable-properties-for-unannotated-sql.md) — open
- [16. Assert and declare string `ENUM` domains](0016-assert-and-declare-string-enum-domains.md) — open
- [17. Complete typed `ENUM` support](0017-complete-typed-enum-support.md) — open
- [18. Assert exhaustive simple `CASE` expressions](0018-assert-exhaustive-simple-case-expressions.md) — open
- [20. Propagate Foreign Keys through queries](0020-propagate-foreign-keys-through-queries.md) — open
- [21. Assert simple `EXACTLY ONE` joins](0021-assert-simple-exactly-one-joins.md) — open (two items after 24)
- [22. Complete `EXACTLY ONE` reasoning](0022-complete-exactly-one-reasoning.md) — open
- [23. Close IR lowering gaps](0023-close-ir-lowering-gaps.md) — open (epic)
- [24. Functional-dependency reasoning](0024-functional-dependency-reasoning.md) — open (epic, planned)
- [25. Check rule consistency invariants](0025-check-rule-consistency-invariants.md) — open (blocked by 24)
- [26. Equality groups](0026-equality-groups.md) — open
- [27. Soundness oracle against DuckDB](0027-soundness-oracle-against-duckdb.md) — open (Part 1 done: `pytest --soundness-oracle`; Part 2 open)

## Order

1. 27 soundness oracle, Part 1 — done. Run it before merging any rules change.
2. 24 functional-dependency reasoning, starting with its 23 prerequisites.
3. Lanes, one at a time in `sqlassert/rules/`:
   - Values: 26 → 16 → 17 → 18
   - Foreign Keys (19, 21 done; 21 leftovers after 24): 26 → 20; 20 → 22
4. Independent, in parallel at any time: 11, 15 (marker suggestions done;
   rewrite suggestions wait for 24). 25 is re-scoped after 24.
