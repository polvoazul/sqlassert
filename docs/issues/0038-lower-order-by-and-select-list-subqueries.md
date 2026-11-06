# 38. Lower ORDER BY and select-list scalar subqueries

- **State:** open
- **Labels:** child of [23](0023-close-ir-lowering-gaps.md)
- **Created:** 2026-11-06
- **Split from:** issue 23
- **Unblocks:** [15](0015-suggest-provable-properties-for-unannotated-sql.md)'s remaining Low smells

## ORDER BY

The IR drops ORDER BY. No Property needs it: ordering changes no row, so no
proof is lost. It matters for suggestions (issue 15):

- An ORDER BY key that earlier keys already determine is redundant (FD
  reduction of the sort list, Paulley §3 "sorting").
- `LIMIT` with an ORDER BY whose keys are not a Unique Set picks arbitrary rows.
  The Arbitrary LIMIT smell reads this from syntax today; with the keys in
  the IR it can ask whether they are unique.

Keep the keys on `Limit` (or a `Sort` node), resolved to columns.

## Scalar subqueries in the select list

`(SELECT x FROM r WHERE r.k = o.k)` in the select list is an
`OpaqueExpression`. Lower it as a left dependent join of the outer input with
r, at most one row per outer row (the subquery errors otherwise), like the
semi joins for EXISTS. The column then gets nullability, domain and FDs, and a
smell fires when at most one row is not proved.
