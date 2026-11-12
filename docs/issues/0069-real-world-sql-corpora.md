# 69. Real-world SQL corpora, run through the oracle as they are

- **State:** open
- **Priority:** P3 (needs corpora): Deferred: needs downloaded corpora. Unblocks
  #11 and #27 Part 2.
- **Theme:** soundness
- **Labels:** testing, corpus
- **Created:** 2026-11-11
- **Related:** [11: crash fuzzing](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md),
  [27: soundness oracle](0027-soundness-oracle-against-duckdb.md) (Part 2),
  [63: random programs](0063-generate-random-programs-for-the-oracle.md),
  [61: VeriEQL oracle](0061-symbolic-oracle-with-verieql.md) (same repository)

## Why

Issues 11 and 27 Part 2 are blocked on a corpus. Hand-written tests only
cover the SQL shapes we thought of; real SQL finds crashes and wrong PROVEDs
we did not.

## What to do

Take all four sources:

1. **DuckDB's `test/sql` logic tests** (MIT). The richest source of join,
   set-operation and subquery shapes, with their own `CREATE TABLE`s.
2. **TPC-H and TPC-DS** DDL and query texts. Keys and foreign keys are
   declared, so many joins should prove. The specification's license is
   less clear than MIT: check before checking any text in.
3. **SQL from public dbt projects** (for example jaffle_shop and dbt's
   example projects), compiled to plain SQL.
4. **VeriEQL's LeetCode benchmark**
   ([VeriEQL/VeriEQL](https://github.com/VeriEQL/VeriEQL),
   `benchmarks/leetcode/leetcode.jsonlines`, CC BY-NC-SA 4.0: fetch, never
   vendor). 23,994 pairs of about 24k distinct hand-written MySQL queries
   over 56 LeetCode problems: one reference solution per problem paired
   with its submissions. Each line carries the schema (column types,
   ENUMs) and constraints (73 primary keys, 31 foreign keys, ranges,
   `in`, `neq`), so render them as `CREATE TABLE` with keys, ENUM and
   CHECK. Shapes: joins in about 15k queries, GROUP BY 14k, subqueries
   8k, CTEs 5k, HAVING 4k, UNION 2k, window functions 800.
   - The constraints are VeriEQL's, not LeetCode's (problem 175 makes
     both `ADDRESSID` and `PERSONID` primary keys of `ADDRESS`): treat
     them as declared facts, and generate oracle data that satisfies them.
   - Everything is upper-cased, string literals and ENUM values too, so
     generated data must use the upper-cased values.
   - Report counts per problem as well as per query: the largest problems
     have about 900 submissions each and would dominate.
   - **Completeness check, new to this source:** the two queries of a pair
     answer the same problem. A Property proved on one but not on the other,
     where VeriEQL found them equivalent, is a missed proof. List those by
     shape to rank completeness work.
   - The same repository has 397 Calcite rewrite pairs and 64 literature
     pairs; those belong to [62](0062-cross-check-suggest-rewrites-with-equivalence-checkers.md).

**Run them as they are.** Do not curate, reduce or mutate the corpora.
Point the existing soundness oracle (`pytest --soundness-oracle`) and the
`suggest` candidate generator at the unchanged files. Record crashes,
diagnostics and every PROVED the oracle refutes. This saves the curation
work and keeps maintenance simple: updating a corpus means bumping a pinned
version, not re-editing fixtures.

- Fetch each corpus at a pinned commit or version into a gitignored
  directory with a script, rather than vendoring it, unless its license
  clearly allows that. This also avoids the TPC licensing question.
- Skip what sqlassert cannot run: logic-test directives, statements the
  oracle's DuckDB rejects, and programs that only error. Report the skip
  counts so a parser regression shows.
- A failure worth keeping becomes a reduced, named `.sqltest` case that
  names its source. The corpus never becomes the regression surface itself
  (issue 11's rule).
- Opt in, outside the default run, like `--soundness-oracle`.

## Acceptance criteria

- [ ] A script fetches the four corpora at pinned versions; provenance and
  license are noted per source.
- [ ] One opt-in command runs `analyze`, `suggest` and the soundness oracle
  over every statement, unchanged, and prints crash, refuted-PROVED and
  skip counts per source.
- [ ] Every crash and refuted PROVED found on the first run is fixed or
  filed.
- [ ] On LeetCode pairs, missed proofs (proved on one query of an
  equivalent pair, not the other) are listed by shape.
- [ ] Issue 11's and 27 Part 2's corpus criteria are ticked or point here.
