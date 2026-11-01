# Issues

Open work, one file per issue. Issues 11 and 14 were migrated from GitHub issues (polvoazul/sqlassert) on 2026-11-01; 16–25 from `TODO.md` and the README Ideas list. 26 and 27 were split out of 16 and 11.

- [11. Fuzz-test analyze against a real-world SQL corpus](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — open
- [15. Suggest provable properties for unannotated SQL](0015-suggest-provable-properties-for-unannotated-sql.md) — open
- [16. Assert and declare string `ENUM` domains](0016-assert-and-declare-string-enum-domains.md) — open
- [17. Complete typed `ENUM` support](0017-complete-typed-enum-support.md) — open
- [18. Assert exhaustive simple `CASE` expressions](0018-assert-exhaustive-simple-case-expressions.md) — open
- [20. Propagate Foreign Keys through queries](0020-propagate-foreign-keys-through-queries.md) — open
- [22. Complete `EXACTLY ONE` reasoning](0022-complete-exactly-one-reasoning.md) — open
- [23. Close IR lowering gaps](0023-close-ir-lowering-gaps.md) — open (epic)
- [27. Soundness oracle against DuckDB](0027-soundness-oracle-against-duckdb.md) — open (Part 1 done: `pytest --soundness-oracle`; Part 2 open)
- [28. Analyze join markers inside CTE bodies](0028-analyze-join-markers-inside-cte-bodies.md) — open

## Order

Done: 14, 19, 21, 24, 26, 27 Part 1, 15 Mid level. Closed: 25 (its targets were deleted by 24; the oracle covers soundness). Run
`pytest --soundness-oracle` before merging any rules change.

Next, as dependencies allow:

1. Values: 16 → 17 → 18.
2. Foreign Keys: 20 → 22.
3. Independent: 15 High level (rewrite suggestions), 28 joins inside CTE, view and subquery bodies.
4. Skipped for now (need downloaded corpora): 11, 27 Part 2.
