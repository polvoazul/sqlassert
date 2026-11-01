# 15. Suggest provable properties for unannotated SQL

- **State:** open
- **Labels:** needs-design
- **Created:** 2026-11-01

## Problem Statement

Adopting sqlassert means writing markers by hand, and an author must first
guess which properties hold. Most authors will not start from a blank page.
sqlassert can already infer facts without any markers: `Report.relation_facts`
lists the Unique Sets proved for every Named Relation.

## What to build

A `suggest` mode: run sqlassert on unannotated SQL and list what it can prove,
each with the marker to paste and its evidence. For example:

```
line 4  JOIN users        Unique Join            /**ASSERT UNIQUE**/
line 9  active_users      (id)  Non-Null Unique Set  /**ASSERT PRIMARY KEY(id)**/
line 9  active_users      (email)  Unique Set       /**ASSERT UNIQUE(email)**/
```

Authors pick the facts they want locked in as `ASSERT`s; a later change that
breaks one then shows up as `UNKNOWN`.

Candidates to try, generated mechanically from the Program IR:

- Every JOIN: Unique Join.
- Every Select Expression (Root Select, view, CTE, FROM subquery): Unique Sets
  and Non-Null Unique Sets over its output columns, and Non-Null columns.

## Suggestion levels

Suggestions are ranked by how directly they improve the SQL:

- **High: native SQL improvements.** Changes that make the query simpler or
  correct on its own, justified by a proof:
  - A LEFT JOIN whose padding a later filter always removes behaves as INNER
    (padding promotion, [`docs/functional-dependencies.md`](../functional-dependencies.md#null-sources-decided-first-cut)):
    suggest `INNER JOIN`. This is the first concrete case, and a code smell:
    the author wrote an outer join that cannot pad.
  - A redundant join: exactly-one, and contributes no columns the query uses.
  - A redundant `DISTINCT`, or a `GROUP BY` column another grouping column
    determines.
- **Mid: add markers.** Lock in what is provable but not asserted, e.g.
  `/**ASSERT PRIMARY KEY(id)**/` (the candidates below).
- **Low: other code smells.** Open.

The High level depends on functional dependencies and null sources
([issue 24](0024-functional-dependency-reasoning.md)).

## Design questions

- Which column sets to report: only minimal ones (drop supersets of a reported
  set), and up to what size?
- Prefer the strongest form: report a Non-Null Unique Set instead of the Unique
  Set it implies.
- Anonymous Select Expressions have no name; identify them by line and marker
  attachment site.
- Output: a structured API result first; CLI text and an "annotate in place"
  rewrite later.
- Only `PROVED` results are suggested; never data-derived guesses.

## Acceptance criteria

- [ ] `suggest(sql)` API returning, per attachment site, the provable
  properties with marker text and evidence.
- [ ] Minimal column sets only, strongest property form only.
- [ ] Pasting every suggested marker into the SQL and running `analyze()`
  proves all of them (round-trip test).
- [ ] CLI entry point printing the suggestions.

## Related

- [0011](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — the
  oracle-based differential testing shares the candidate generator.
