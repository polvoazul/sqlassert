# 15. Suggest provable properties for unannotated SQL

- **State:** open (Mid level done)
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

## Design decisions (Mid level, built)

- **Minimal sets, size bound.** Only minimal Unique Sets are suggested: a
  superset of a proved set is dropped. Sets are searched exhaustively,
  smallest first, up to `max_size` columns (default **3**). Unique Sets come
  from declared keys, `GROUP BY`, `DISTINCT`, and `QUALIFY` partitions, and
  composites past three columns are rare; meanwhile the search grows as
  C(n, k) (a 40-column select: ~10k sets to size 3, ~100k to size 4, about
  1.3 s at size 3 today). To not miss a wide key outright, the set of all
  output columns is also tried, and when it is unique and no smaller set is,
  it is shrunk greedily to a minimal set. Proof is monotone in the column set,
  so a select whose whole output is not unique skips the search entirely.
  This finds one, not every, minimal set wider than `max_size`.
- **Strongest form.** A minimal set is suggested as a Non-Null Unique Set
  (`/**ASSERT NOT NULL UNIQUE(...)**/`) when that is proved, else as a Unique
  Set. A nullable minimal set cannot have a non-null superset proved, so
  minimality is taken over Unique Sets. The canonical `NOT NULL UNIQUE` is
  used rather than the `PRIMARY KEY` alias, which reads as "the one key".
  Non-null columns become one `/**ASSERT NOT NULL(...)**/` per site, leaving
  out columns a suggested Non-Null Unique Set already makes non-null: nothing
  suggested at a site is implied by another suggestion there.
- **Attachment Sites.** Each suggestion names its site by line plus a label:
  `JOIN users AS u`, `view v`, `CTE c`, `subquery s`, or `Root Select`. The
  line is where the marker goes (a column marker trails its Select
  Expression, so it is the Select's last line). Each member of a set
  operation is a site of its own. A comma join has no site.
- **Output.** `suggest(sql, *, dialect, max_size) -> SuggestionReport` with
  `suggestions` (site, `property`, `marker`, `evidence`), `diagnostics`, and
  `annotated()`, which pastes markers in without moving any line. The CLI
  `sqlassert suggest FILE [--explain]` (also `python -m sqlassert`) prints one
  line per suggestion; diagnostics go to stderr and exit 1.
- **Only PROVED.** Candidates are proved with `Arena.prove` on the
  unannotated program; nothing is data-derived. Properties already asserted
  or declared are not suggested again. A program with a parse failure,
  unattached marker, or duplicate declaration gets no suggestions, since
  `analyze` would prove none of them.
- **Columns a marker cannot name** (unnamed, or a name repeated in the
  output, as in `SELECT a.id, b.id`) take no part in candidates.
- **Shared generator.** `candidates(lower_with_sites(sql), max_size=...)`
  yields every candidate, proved or not, with its marker; `annotate(sql,
  items)` pastes any of them. Issue 27 Part 2 reuses both.

## Acceptance criteria

Mid level ("add markers"):

- [x] `suggest(sql)` API returning, per attachment site, the provable
  properties with marker text and evidence.
- [x] Minimal column sets only, strongest property form only.
- [x] Pasting every suggested marker into the SQL and running `analyze()`
  proves all of them (round-trip test).
- [x] CLI entry point printing the suggestions.

Still open:

- [ ] High level: `INNER JOIN` for a LEFT JOIN that cannot pad, redundant
  joins, redundant `DISTINCT` / determined `GROUP BY` columns. Needs
  [issue 24](0024-functional-dependency-reasoning.md).
- [ ] Low level: other code smells.
- [ ] Annotate in place (a CLI flag over `SuggestionReport.annotated()`).

## Related

- [0011](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — the
  oracle-based differential testing shares the candidate generator.
