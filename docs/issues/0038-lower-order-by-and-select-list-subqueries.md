# 38. Lower ORDER BY and scalar subqueries

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

## Scalar subqueries, in WHERE and in the select list

A scalar subquery is an `OpaqueExpression` today, which may differ from row to
row. Lower it as a join with its body B instead:

- **Uncorrelated:** `WHERE t.a = (SELECT max(x) FROM r)` joins `t` with `B`,
  and the subquery's value is `B.m`.
- **Correlated:** `(SELECT r.x FROM r WHERE r.k = o.k)` is a dependent join on
  the correlation equalities (`r.k = o.k`), like the semi joins for EXISTS.
- **Join kind: left.** No row makes the subquery NULL, which is exactly a left
  join's padding, wherever the subquery appears. An inner join is equivalent
  only when what contains the subquery rejects NULL in it: `a = (…)` drops the
  row either way, but `(…) IS NULL`, `coalesce((…), 0) = a` or `a = (…) OR b = 1`
  do not, and an inner join would drop rows they keep. So lower a
  left join and let the existing outer-join reasoning see that a WHERE
  conjunct strict in `m` makes it behave as an inner join.

**At most one row.** More than one row is a runtime error, so wrap B in
`Limit(B, 1)`: sound, since LIMIT 1 changes the result only when the query
would have failed. The existing rules then give the rest:

- Uncorrelated: `Limit 1` has the empty set as a Unique Set (C3), so `∅ → m`:
  m is the same on every row, the value or NULL on all of them when B is
  empty. `WHERE a = m` rejects NULL in m and puts `a` in its Equality Group:
  `∅ → a`. This is Paulley's R10 (moved here from issue 37), with no rule of
  its own. Check that the padded side keeps `∅ → m` through the left join
  (it should: a one-row-or-empty input pads all rows or none).
- Correlated: `k → a` from the equalities, through the same FD rules.

**Smell (issue 15's "scalar subquery not proved to return at most one row").**
The lowered join is a left Unique Join: at most one B row per outer row.
Judge that with the existing `unique_join` on the join over B before wrapping
B in LIMIT 1 (or the LIMIT proves it trivially). One check covers both cases:

- Correlated: the correlation equalities must bind right columns that reach
  B's Row Identity.
- Uncorrelated: the join has no conjuncts, so, as for a cross join, B itself
  must have at most one row (the empty set is a Unique Set of B: a scalar
  aggregate, a `LIMIT 1`, a filter pinning a key).

When it is not proved, report that the subquery may return more than one row
(a runtime error), naming the columns that would have to be unique.

### Implementation sketch

1. **Lowering.** A scalar subquery with body B lowers to
   `Join(kind="left", left=outer, right=Limit(B, count=1), predicate=correlation equalities)`
   (no predicate when uncorrelated); its value is B's column. Every downstream
   proof sees the Limit.
2. **Implied Limit.** `Limit` gets a field such as
   `source: Literal["clause", "scalar subquery"]`, so the smell can find these
   joins and the Arbitrary LIMIT smell (`smells._arbitrary_limit`) never reads
   an implied Limit as one the author wrote. (It only fires for a Limit at a
   SELECT's attachment site, which the implied one is not; the field makes it
   explicit.)
3. **Smell.** For each reachable `Join` whose right input is a
   `source="scalar subquery"` Limit, judge the same join over bare B:
   ```python
   check = dataclasses.replace(join, right=join.right.input)
   result = unique_join(check)
   ```
   `check` is built only for this question and nothing consumes it. The
   engine memoizes per node, so B's facts are already computed. When
   `result` is not proved, report the smell, naming the columns its closure
   leaves loose (as Arbitrary LIMIT does).
4. **Check while implementing:** that analyses run on a node outside
   `program.nodes()` with the arena active (`smells` already calls `_closure`
   that way). If not, build `check` during lowering and record it in the
   program next to the attachment sites.
5. **Later:** when `unique_join(check)` is proved, the implied Limit adds
   nothing, which is the same judgement a "redundant LIMIT 1" suggestion needs.
