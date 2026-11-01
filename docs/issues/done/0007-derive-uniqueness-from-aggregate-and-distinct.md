# 7. Derive uniqueness from Aggregate and Distinct

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/7

## Parent

#1

## What to build

Derive Unique Sets from ordinary Aggregate and Distinct operations so aggregate and deduplicated subqueries can prove outer Unique Join Assertions. Keep Grouping Keys distinct from Aggregate Expressions and preserve bound expression identity through projection into the outer query.

## Acceptance criteria

- [ ] An ordinary aggregate result is treated as unique by the complete set of its Grouping Keys.
- [ ] A GROUP BY subquery joined on every Grouping Key returns PROVED.
- [ ] A GROUP BY subquery joined on only part of its Grouping Keys returns UNKNOWN.
- [ ] Grouping Keys may be bound scalar expressions and map correctly to projected output expressions when supported.
- [ ] Aggregate Expressions such as sums and counts do not themselves contribute to the grouping Unique Set.
- [ ] SELECT DISTINCT establishes a Unique Set over its complete output expression set.
- [ ] Joining a DISTINCT subquery on only part of its distinct output set remains UNKNOWN.
- [ ] Grouping sets, ROLLUP, CUBE, and unsupported aggregate semantics remain explicit and conservative.
- [ ] Automated tests through `analyze` include happy paths for full GROUP BY and DISTINCT coverage and unhappy paths for partial coverage, aggregate-output misuse, and excluded grouping semantics.

## Blocked by

- #5

---

**Comment by polvoazul on 2026-08-27:**

Delivered by commit 39c5444, with subsequent rule refinements, and covered by Aggregate and Distinct proof and soundness tests. Current verification: 236 tests pass and ty check passes. Closing as complete.
