# 15. Suggest provable properties for unannotated SQL

- **State:** open (rewrites and markers done)
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

Suggestions come in three types, listed in this order:

- **Rewrites: native SQL changes.** Changes that make the query simpler or
  correct on its own, justified by a proof:
  - A LEFT JOIN whose padding a later filter always removes behaves as INNER
    (padding promotion, [`docs/functional-dependencies.md`](../../functional-dependencies.md#null-sources-decided-first-cut)):
    suggest `INNER JOIN`. This is the first concrete case, and a likely mistake:
    the author wrote an outer join that cannot pad.
  - A redundant join: exactly-one, and contributes no columns the query uses.
  - A redundant `DISTINCT`, or a `GROUP BY` column another grouping column
    determines.
  - A CTE nothing references: dead code. Today it is never lowered, so any
    marker in it goes unchecked as well.
- **Markers.** Lock in what is provable but not asserted, e.g.
  `/**ASSERT PRIMARY KEY(id)**/` (the candidates below).
- **Hazards: results the SQL leaves unspecified.** Open. First step: triage the Kossmann survey
  (below) to build the list.

Rewrites depend on functional dependencies and null sources
(issue 24 (done; see [functional-dependencies.md](../../functional-dependencies.md))).

## Design decisions (markers, built)

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
- **Sites live on the IR.** Normal lowering records every site in
  `Program.attachment_sites`: a plain-valued `AttachmentSite` (label, line,
  offset, trailing) paired with the IR Join or Select relation. No SQLGlot
  node enters the IR, and `suggest` needs no hook into `IrParser`.
- **Shared generator.** `candidates(program, dialect=..., max_size=...)`
  yields every candidate, proved or not, with its marker; `annotate(sql,
  items)` pastes any of them. Issue 27 Part 2 reuses both.

## Design decisions (rewrites, built)

- **Rewrites, not markers.** `SuggestionReport.rewrites` holds
  `Rewrite`s (`sqlassert/rewrites.py`): site, `rewrite` kind, a plain
  `message` saying what to write, and `evidence`. Each carries `level`
  (`SuggestType.REWRITE`); marker `Suggestion`s carry `SuggestType.MARKER`. The CLI prints a
  type column and lists every Rewrite before any marker.
- **Result-preserving, as a multiset.** Every rewrite keeps the program's
  result row for row. `tests/test_suggest_rewrites.py` checks each kind
  against DuckDB: original and rewritten SQL on random tiny instances
  (the soundness oracle's generator), compared as multisets.
- **INNER JOIN.** A LEFT join proved Exactly One; or a LEFT or RIGHT join
  whose padding a null-rejecting conjunct removes (padding promotion): a later
  join's ON, or the WHERE, of the same Select Expression. The walk stops at
  the Select's edge: past it a LIMIT, window, or aggregate may see padded rows
  first, and a CTE's or view's body has other readers.
- **Redundant / filter-only join.** No column of the right side is used
  outside the join's own ON, read from the syntax (the IR drops ORDER BY and
  hides columns inside opaque expressions). Any reference by the right side's
  alias, any unqualified name among its columns, `*`, USING, or NATURAL counts
  as a use. A unique LEFT join or an Exactly One inner join is removable; a
  unique inner join only filters (`WHERE EXISTS`), unless a RIGHT or FULL join
  follows: the filter would then act after it.
- **DISTINCT / GROUP BY.** The strict closure (`definite = ∅`, NULL equal to
  NULL) of the selected column references in the input reaches its Row
  Identity; a lax Unique Set is not enough (`SELECT DISTINCT email` collapses
  NULLs). A grouping column is redundant when the grouping columns kept so far
  strictly determine it, greedily, keeping at least one.
- **Unused CTE.** Syntactic: a CTE no query of its WITH reaches, through used
  CTEs only. A same-named table anywhere counts as a reference.
- **Marker additions.** A join is suggested as `EXACTLY ONE` when proved, else
  `UNIQUE`; a join that already carries a marker gets neither.
- **ENUM.** `/**ASSERT ENUM(col IN (...))**/` for every nameable output
  column whose proved Enumerated Domain has 1 to `MAX_ENUM_VALUES` (10)
  values, listed booleans, then numbers by value, then strings. A short closed
  list is a category worth locking in; a longer one mostly restates a long
  `IN` list, and the marker repeats at every site the column passes through.
  A column written as a literal gets none: the marker would restate it. The
  values come from the domain analysis (`rules.domain._domain`), so ENUM is
  not among `candidates`; the marker is then proved like any other.
- **EXHAUSTIVE.** Every simple CASE that is lowered is an Attachment Site
  (label `CASE <operand>`, offset right before `CASE`). `/**ASSERT
  EXHAUSTIVE**/` is suggested there when proved, the CASE is reachable and
  unmarked, and every WHEN is a distinct non-null literal, as the marker's
  grammar requires. A CASE inside an aggregate, window or subquery is not
  lowered, so it gets no site.

## Acceptance criteria

Markers:

- [x] `suggest(sql)` API returning, per attachment site, the provable
  properties with marker text and evidence.
- [x] Minimal column sets only, strongest property form only.
- [x] Pasting every suggested marker into the SQL and running `analyze()`
  proves all of them (round-trip test).
- [x] ENUM and EXHAUSTIVE markers suggested, and in the round-trip test.
- [x] CLI entry point printing the suggestions.

Rewrites:

- [x] `INNER JOIN` for a LEFT JOIN that cannot pad, redundant joins,
  redundant `DISTINCT` / determined `GROUP BY` columns, unused CTEs, each
  checked for equivalence against DuckDB.

Still open:

- [x] Triage Kossmann et al. 2022, Table 1
  (`papers/2022-kossmann-data-dependencies-for-query-optimization-survey.pdf`):
  about 60 dependency-based optimizations, by dependency (UCC 17, FD 14,
  OD 18, IND 10) and operator. Go through every entry and record, in a table
  in this issue: the entry, its section, and one of *covered* (name the
  sqlassert feature), *add* (as an assertion, a rewrite, or a
  hazard, with its precondition as a Property), or *not applicable*
  (why: data-dependent, cost-based, physical). Each rewrite precondition is a
  property sqlassert can prove ([literature-notes.md](../../literature-notes.md),
  "Ideas to adopt"). Order dependencies (ORDER BY / sort elimination) are
  untouched so far.
  Done: see "Kossmann Table 1 triage" below.
- [ ] Hazards: the rest, from the *add* rows of that triage.
  First hazard done: Arbitrary LIMIT (`sqlassert/hazards.py`, `report.hazards`,
  `SuggestType.HAZARD`, printed last by the CLI). Issue 38 added the scalar subquery
  hazard and the redundant ORDER BY key (a rewrite, `Redundant ORDER BY
  key`); issue 41 the Fan Trap and Chasm Trap. Done since: redundant `EXISTS`,
  `EXCEPT ALL` / `INTERSECT ALL`, whole `GROUP BY` as a projection, redundant
  `LIMIT`/`ORDER BY` on one row, self-join on a key, implied conditions, and
  the always-empty hazard. UNION and INTERSECT simplification under coverage
  are not planned (rare).
- [x] Annotate in place (a CLI flag over `SuggestionReport.annotated()`).
  Done: `sqlassert suggest --annotate FILE` rewrites FILE with every
  suggested marker; for `-` it prints the annotated SQL instead of the list.

## Kossmann Table 1 triage

Kossmann et al. 2022, Table 1: 59 entries (UCC 17, FD 14, OD 18, IND 10).
Marks: ∗ cost-independent rewrite, † cost-based, ‡ estimation. *Covered*
names the sqlassert feature; *add* names the level and the Property that is
its precondition; *n/a* says why. Every † and ‡ entry is *n/a*: sqlassert
reasons about the query, not its plan, cost or cardinality.

### Unique Column Combinations (Sec. 5)

| Entry | Area | Mark | Triage |
|---|---|---|---|
| Spurious-free back-joins [129] | Join | ∗ | covered: Unique Join / Exactly One Join Assertions |
| Semijoin transformation [89] | Join | ∗ | covered: rewrite "filter-only join" (`WHERE EXISTS`) |
| Pipeline with grouping [30, 126] | Join | † | n/a: physical pipelining |
| Invisible join [2] | Join | † | n/a: column-store execution |
| Early abort (5.6) | Selection | ∗ | done, rewrite `LIMIT or ORDER BY on one row`: on a Select proved to return at most one row (the empty set is a strict key); not with OFFSET or a LIMIT of 0 or unknown count |
| Accurate cardinalities (5.6) | Selection | ‡ | n/a: estimation |
| Grouping is redundant [23] | Grouping | ∗ | done, rewrite `GROUP BY of one row per group`: grouping columns whose strict closure reaches the input's Row Identity make the query a projection; each aggregate becomes its one-row value (`count(*)` → 1, `count(x)` → CASE, `sum`/`avg`/`min`/`max`/`any_value`/`bool_and`/`bool_or` → x); result types may change. Silent for FILTER, an aggregate in ORDER BY, QUALIFY or a window, and other aggregates |
| Accurate cardinalities (5.6) | Grouping | ‡ | n/a: estimation |
| Avoid DISTINCT [101–103] | Projection | ∗ | covered: rewrite "redundant DISTINCT" |
| Reduce attributes (5.6) | Sorting | ∗ | add, rewrite: `ORDER BY` keys after a prefix that is a Unique Set are redundant. Done in issue 38, as a case of the row below |
| Unstable sorting (5.6) | Sorting | ∗ | add, hazard: `ORDER BY … LIMIT` whose sort keys are not proved a Unique Set picks arbitrary rows among ties (the inverse precondition) |
| EXCEPT to EXCEPT ALL [102] | Set ops | ∗ | done, rewrite `EXCEPT ALL or INTERSECT ALL`: when the left input is unique on all columns (strict), where the dialect has it (`set_operations_all`) |
| INTERSECT to INTERSECT ALL [60, 101] | Set ops | ∗ | done: same, for either input of `INTERSECT` |
| INTERSECT to join [101, 102] | Set ops | † | n/a: cost-based |
| Accurate cardinalities (5.6) | Set ops | ‡ | n/a: estimation |
| Subquery to join [101, 102] | Other | ∗ | covered: WHERE subqueries lowered as semi joins (issue 23); not a suggestion, the join form is not simpler SQL |
| Subquery sort avoidance [110] | Other | † | n/a: physical |

### Functional Dependencies (Sec. 6)

| Entry | Area | Mark | Triage |
|---|---|---|---|
| Simplification / avoidance [65] | Join | ∗ | covered: rewrite "redundant join" |
| Complexity reduction (6.2) | Join | ∗ | done, rewrite `Implied condition`, in ON and WHERE: an equality the other equalities chain, or whose sides are pinned to one value, or that equates a self-join's copies of one column; `x = v` / `x IN (...)` covering x's Enumerated Domain. Deleted when its columns are non-null, else replaced by `x IS NOT NULL` |
| Self-join avoidance [6] | Join | ∗ | done, rewrite `Self-join on a key`: a self-join on a non-null key reads each row twice; read the first copy instead (other ON conjuncts to WHERE, or `CASE WHEN` per column for a LEFT JOIN). Views and CTEs when reading them twice gives the same rows and the columns read are pure |
| Plan generation [38] | Join | † | n/a: join ordering |
| Early abort (6.3) | Selection | ∗ | n/a: execution-time abort |
| Substitute attributes [24, 68] | Selection | † | n/a: index choice |
| Estimations without independence [25, 58, 108] | Selection | ‡ | n/a: estimation |
| Reduce attributes [17, 116] | Grouping | ∗ | covered: rewrite "determined GROUP BY column" |
| Distinctness: see grouping [123] | Projection | ∗ | covered: rewrite "redundant DISTINCT" (closure) |
| Simplification [29] | Projection | ∗ | covered: the same closure |
| Estimate projections [44] | Projection | ‡ | n/a: estimation |
| Reduce attributes [24, 112, 116] | Sorting | ∗ | add, rewrite: an `ORDER BY` key earlier keys determine (strictly) is redundant. Done in issue 38 (Redundant ORDER BY key) |
| Scalar subqueries [29] | Other | — | add, hazard: a scalar subquery not proved to return at most one row (precondition: its correlated columns are a Unique Set, as a Unique Join; done in issue 38) |
| Table decomposition rewrites [45] | Other | — | n/a: physical schema design |

### Order Dependencies (Sec. 7)

No Order Dependency Property exists and ORDER BY is not lowered, so every
entry is *n/a* for now (P4: Paulley does not cover order). The ∗ entries
(join avoidance [117, 119], simplify MIN/MAX/MEDIAN [95, 102], reduce sort
attributes [115–117], avoid sort [49, 102], ORDER BY with index [117]) would
become *add* once an Order Dependency Property exists; the † and ‡ entries
(pipeline with grouping, avoid sort for sort-merge joins, attribute
substitution, pipeline index scan with join, binary search, sort-based
grouping, distinctness via sorting, main-memory sorts, substitute attributes,
accurate estimates, set-operation order optimizations, correlated subqueries,
sparse over dense indexes) are physical or estimation.

### Inclusion Dependencies (Sec. 8)

| Entry | Area | Mark | Triage |
|---|---|---|---|
| Join elimination [24, 64] | Join | ∗ | covered: Exactly One Join via Foreign Keys, rewrite "redundant join" |
| Substitute relations [33] | Join | † | n/a: cost-based |
| Avoid semijoin reductions (8.1) | Join | † | n/a: distributed execution |
| Accurate cardinalities [56] | Join | ‡ | n/a: estimation |
| Simplify UNION (8.2) | Set ops | ∗ | not planned (rare), was add, rewrite: `A UNION B` is `B` (distinct) when A's columns are covered by B's (precondition: Referential Coverage of the whole row, and NULL-free A: MATCH SIMPLE exempts NULLs that UNION keeps) |
| Simplify INTERSECT (8.2) | Set ops | ∗ | not planned (rare), was add, rewrite: `A INTERSECT B` is `SELECT DISTINCT` A under the same precondition |
| Eliminate EXCEPT (8.2) | Set ops | ∗ | done, hazard `Always empty`: `A EXCEPT B` under that coverage, and the orphan checks `NOT EXISTS` and `LEFT JOIN ... IS NULL` (any key the SQL states counts; enforcement is not asked) |
| Accurate cardinalities (8.2) | Set ops | ‡ | n/a: estimation |
| Query folding [33, 51, 57] | Other | ∗ | n/a: reuses other queries' results |
| Eliminate correlated subqueries in EXISTS [85] (8.2) | Other | ∗ | covered (Redundant EXISTS, via Exactly One); was add, rewrite: an `EXISTS`/`IN` filter every row passes (the semi join's at-least-one half: coverage onto the ON columns, non-null, only cross-input equalities) is redundant |

Chosen first (no new lowering needed): the redundant `EXISTS`/`IN` filter
(rewrite) and the arbitrary `ORDER BY … LIMIT` (hazard).

## Related

- [0011](../0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — the
  oracle-based differential testing shares the candidate generator.
