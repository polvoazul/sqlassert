# Issues

Open work, one file per issue. Issues 11 and 14 were migrated from GitHub issues (polvoazul/sqlassert) on 2026-11-01; 16–25 from `TODO.md` and the README Ideas list. 26 and 27 were split out of 16 and 11.

- [11. Fuzz-test analyze against a real-world SQL corpus](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — open
- [15. Suggest provable properties for unannotated SQL](0015-suggest-provable-properties-for-unannotated-sql.md) — open
- [23. Close IR lowering gaps](0023-close-ir-lowering-gaps.md) — open (epic)
- [27. Soundness oracle against DuckDB](0027-soundness-oracle-against-duckdb.md) — open (Part 1 done: `pytest --soundness-oracle`; Part 2 open)
- [30. Read enum types and collations from the catalog](0030-read-enum-types-and-collations-from-the-catalog.md) — open
- [31. Enumerated Domains through set operations](0031-enumerated-domains-through-set-operations.md) — open
- [32. Infer simple CASE nullability](0032-infer-simple-case-nullability.md) — open
- [33. INNER JOIN suggestion across join chains and CTEs](0033-inner-join-suggestion-across-join-chains-and-ctes.md) — open
- [34. Close the soundness oracle's blind spots](0034-close-soundness-oracle-blind-spots.md) — open

## Order

Done: 14, 16, 17, 18, 19, 20, 21, 22, 24, 26, 28, 29, 27 Part 1, 15 Mid and High levels. Closed: 25 (its targets were deleted by 24; the oracle covers soundness). Run
`pytest --soundness-oracle` before merging any rules change.

Next, as dependencies allow:

1. In progress: 23 small items (silent markers, `count` non-null, `ALTER TABLE`/unique-index constraints) and `suggest` ENUM/EXHAUSTIVE markers.
2. Follow-ups: 30–34 (independent of each other).
3. 15 Low level (needs a list of smells), 23 medium and large items.
4. Skipped for now (need downloaded corpora): 11, 27 Part 2.
