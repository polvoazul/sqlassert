# 71. Rewrites carry exact edits

- **State:** done (2026-11-12)
- **Priority:** P2 (open): a Rewrite's prose and the tests' hand-written rewritten SQL can drift apart.
- **Theme:** suggestions
- **Labels:** suggest, testing
- **Created:** 2026-11-12
- **Related:** [62: cross-check rewrites](../0062-cross-check-suggest-rewrites-with-equivalence-checkers.md) (split from it), [15: suggest](0015-suggest-provable-properties-for-unannotated-sql.md)

## Why

A Rewrite says what to change in prose ("write JOIN", "drop GROUP BY and
write SELECT ..."), and `tests/test_suggest_rewrites.py` compares the
original against a rewritten query written by hand. Nothing ties the two
together: the message, the hand-written query and what a user would type can
all differ. Issue 62's triple export also needs the exact rewritten SQL.

## What to do

1. Each Rewrite carries text edits (start, end, replacement) on the original
   SQL, placed from SQLGlot's token positions (the AST does not keep every
   clause's span). An edit never deletes a comment or a Property Marker
   inside the span it replaces.
2. Everything shown is derived from the edits: the CLI prints the reason and
   a unified diff; the Python API exposes the edits and the edited SQL; the
   dbt report shows the diff against the compiled SQL. A Rewrite with Caveat
   still shows its caveat.
3. The equivalence tests take the Rewrite `suggest` offers, apply its edits,
   and compare on random instances, with the caveat `typeof` checks where
   they exist. The guard tests stay. One hand-written expected diff per
   RewriteKind stays as independent truth.

## Acceptance

- [x] Every RewriteKind has exact edits, or is listed here with why not.
- [x] No hand-written rewritten SQL in `EQUIVALENT`.
- [x] CLI, Python API and dbt report show the edits.

## Closing

Done, one commit per kind (the one-span deletions together). Every
RewriteKind has edits; a Rewrite without them is an error, and one whose
syntax the tokens cannot place is not offered.

| Kind | Edits |
|---|---|
| INNER JOIN | delete LEFT or RIGHT, and OUTER |
| Redundant Join | delete the join |
| Filter-Only Join | delete the join; AND `EXISTS (SELECT 1 FROM <relation> WHERE <ON>)`, both as written, into WHERE (its condition parenthesized when an OR), or write a new WHERE where the FROM ends |
| Redundant DISTINCT | delete DISTINCT |
| Redundant GROUP BY | delete the column and a comma from GROUP BY; wrap its select item in `any_value(...)`, keeping its name |
| GROUP BY of one row per group | delete GROUP BY; replace each aggregate call by `1`, `CASE WHEN x IS NULL THEN 0 ELSE 1 END` or its argument, parenthesized inside an expression unless atomic |
| Redundant ORDER BY key | delete the key and the comma before it |
| Redundant EXISTS | delete the WHERE conjunct and its AND (the whole WHERE when alone), or the SEMI JOIN |
| EXCEPT ALL or INTERSECT ALL | write ALL after the operator, or in place of DISTINCT |
| LIMIT or ORDER BY on one row | delete the clauses |
| Self-join on a key | delete the join; qualify each second-reference column with the first (a LEFT JOIN's guarded by `CASE WHEN`); AND the other ON conjuncts, requalified as written, into WHERE |
| Implied condition | delete the conjunct and its AND (an ON's only one becomes TRUE), or replace it by the IS NOT NULL it still checks |
| Unused CTE | delete it with a comma, or the whole WITH |

- Spans come from the parser: Joins, a Select's first token, query clauses,
  function calls and a plain DISTINCT record their source offsets; lists are
  split at top-level commas, conditions at top-level ANDs.
- An IR conjunct or semi join is matched to its syntax by its written text.
- Messages give the reason and a short instruction; the GROUP BY of one row
  per group, the filter-only join and the self-join no longer spell out the
  rewritten SQL, which the diff shows.
- `sqlassert suggest` prints each Rewrite's unified diff; the dbt report
  prints it against the model's compiled SQL.
- `EQUIVALENT` holds (original, kind); the test runs the Root Select of the
  SQL each offered Rewrite's edits make, as written. One hand-written diff
  per kind is the independent truth.
