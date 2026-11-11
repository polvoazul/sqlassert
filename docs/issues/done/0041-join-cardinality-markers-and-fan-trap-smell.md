# 41. Join cardinality markers and the fan-trap smell

- **State:** done (2026-11-08)
- **Labels:** markers, suggestions
- **Created:** 2026-11-08
- **Related:** [15: suggestions and smells](0015-suggest-provable-properties-for-unannotated-sql.md), [21](0021-assert-simple-exactly-one-joins.md) and [22](0022-complete-exactly-one-reasoning.md): Exactly One

## Why

`/**ASSERT UNIQUE**/` before a join is directional today: the right input is
unique on the join key, so no left row is repeated (many to one). On
`customers JOIN orders USING (c_id)` it is false, though the join is
well-behaved: one side is unique on the key, so only the other side's rows
repeat. To prove it, the author has to put the side with many rows on the
left. That is bad, since join order carries meaning: `customers LEFT JOIN orders`
keeps customers with no orders, and in a chain the left input is everything
joined so far, not a table that can be moved.

The commoner question is "is this join many to many?", the fan trap:
`orders JOIN payments USING (c_id)` repeats both sides, and every sum over it
is multiplied. When a direction matters, the author should be able to say
which one, in the order they wrote the join.

## Markers

Each goes immediately before the join, like today's join markers. "Left" is
everything before the join, "right" its joined source; the words read in
FROM order.

| Marker | Proved when | Rows repeated |
|---|---|---|
| `ASSERT MANY TO ONE` | the right input is unique on the join key (today's `UNIQUE`) | right only |
| `ASSERT ONE TO MANY` | the left input is unique on the join key | left only |
| `ASSERT ONE TO ONE` | both are | none |
| `ASSERT NOT MANY TO MANY` | either is | one side at most |
| `ASSERT UNIQUE` | alias of `NOT MANY TO MANY` (**changes today's meaning**) | one side at most |

`EXACTLY ONE` is unchanged: many to one plus guaranteed matching.

Proofs reuse `unique_join` on the join and on its mirror (inputs swapped,
padding sides swapped), so no rule changes are needed. Unique on any subset of
the equated columns suffices, through the closure, as today. Padding never
repeats a row, so LEFT, RIGHT and FULL joins take the same proofs; NULL keys
match nothing. Whether the join drops rows is a separate question that
`EXACTLY ONE` answers.

Semi and anti joins don't participate: they keep or drop left rows whole and
output no right rows, so cardinality says nothing about them. Every
cardinality marker on one, `UNIQUE` included, is rejected with a diagnostic
(today `UNIQUE` on a SEMI or ANTI join is PROVED).

When `NOT MANY TO MANY` (or `UNIQUE`) is proved, the explanation names the
side that was unique, so the author gets the direction without asserting it.

### Changing `UNIQUE`

`UNIQUE` becomes `NOT MANY TO MANY` because that is the commoner intent.
It is weaker than before: `orders /**ASSERT UNIQUE**/ JOIN order_items ON
i.order_id = o.id` was UNKNOWN and becomes PROVED (orders is unique on `id`),
though `sum(o.total)` over it is multiplied. That is the intended change; the
directional markers and the smell below cover the aggregate case. Alpha, so
no compatibility path:

- Tests use the most specific marker that states their case: `MANY TO ONE`
  (most of today's `UNIQUE` tests), `ONE TO MANY` or `ONE TO ONE`.
  `NOT MANY TO MANY` is their disjunction, so a proof of either direction
  proves it. `UNIQUE` / `NOT MANY TO MANY` gets end-to-end tests covering the
  disjunction's four cases (left unique, right unique): right only PROVED,
  left only PROVED, both PROVED, neither UNKNOWN. SEMI and ANTI cases
  (e.g. in `test_analyze_join_kinds.py`) move to the new diagnostic.
- README "Unique Join Syntax", the CONTEXT.md glossary (Unique Join
  Assertion; add the cardinality terms), and `JOIN_MARKER_NOUNS` in
  `sql_parse.py`.
- `suggest` and `--annotate` (`suggest._candidate`): suggest the strongest
  proved form, `EXACTLY ONE` > `ONE TO ONE` > `MANY TO ONE` or `ONE TO MANY`.
  `NOT MANY TO MANY` is implied by each, so it is never suggested on its own.

## The fan-trap smell (Low, issue 15)

With no marker, flag an aggregate whose input repeats the rows its argument
comes from:

- **Fan trap:** `sum(p.amount)` over `orders JOIN payments USING (c_id)`;
  the join is many to many.
- **Chasm trap:** `sum(c.credit_limit)` over `customers JOIN orders USING (c_id)`;
  the join is one to many, and the summed column is on the side that has
  one row per key.

One check covers both: for an aggregate argument whose columns all come from
one relation instance R with a known Unique Set K, the smell fires when K
(lifted through the joins) is not a Unique Set of the aggregate's input. That
is "R's rows may be repeated before they are summed".

Decide while implementing:

- **Which aggregates:** all of them. `min`, `max` and `DISTINCT` aggregates
  happen to return the right value over repeated rows, but the join is
  wrong either way. `count(*)` has no argument relation, so it fires on the
  fan trap only: no input relation keeps its rows unrepeated.
- **Semi and anti joins** never repeat rows, so they never cause the smell.
- **Silence on unknowns:** fire only when R has a known Unique Set (declared,
  a key, or proved). With no known key there is nothing to lose, and firing
  would be noise.
- **Bridges:** many-to-many through a bridge table is sometimes intended
  (weighting). A Low smell, so the message says the value may be counted
  more than once and names the join that repeats R's rows.
- **Arguments over several relations:** `sum(o.qty * p.price)` mixes sides.
  Skip it at first, or require every contributing relation's key.
- **Through CTEs and subqueries:** the aggregate's input may be a CTE that
  does the join; the check is on facts, so it should work unchanged.

Each case needs a DuckDB test showing the multiplied total, so the smell's
claim is checked on data, not only reasoned.

## Acceptance criteria

- [x] `MANY TO ONE`, `ONE TO MANY`, `ONE TO ONE`, `NOT MANY TO MANY` parse,
  attach before a join, and are proved by `unique_join` and its mirror.
- [x] `UNIQUE` means `NOT MANY TO MANY`; tests use the most specific marker,
  plus end-to-end `UNIQUE` / `NOT MANY TO MANY` tests for right only, left
  only, both (PROVED) and neither (UNKNOWN); README, CONTEXT.md and
  suggestions updated.
- [x] A cardinality marker on a SEMI or ANTI join is rejected with a diagnostic.
- [x] Explanations of `NOT MANY TO MANY` name the unique side.
- [x] The fan-trap/chasm-trap smell fires on both examples, stays silent on
  `sum(o.amount)` over `customers JOIN orders` and on relations without a
  known key; it fires on `min`, `max` and `DISTINCT` aggregates too, and on
  `count(*)` over a many-to-many join.
- [x] `pytest --soundness-oracle` passes; the oracle judges the new markers.

## Done

- Markers parse as their own tokens (`NOT MANY TO MANY` is the same token as
  `UNIQUE`); `rules/join_cardinality.py` proves `many_to_one_join` (today's
  rule), `one_to_many_join` (the same, with the other input matched),
  `one_to_one_join` and `unique_join` (their disjunction, whose explanation
  names the unique input). Internal callers (Exactly One, FDs, improvements)
  use `many_to_one_join`.
- A cardinality marker on a SEMI or ANTI join gets
  `cardinality-on-filtering-join` and no assertion.
- Tests use the most specific marker; `test_analyze_join_cardinality.py`
  covers the disjunction's four cases. The oracle checks each direction by
  counting each input's rows against the join made LEFT or RIGHT.
- `Aggregate.calls` keeps each aggregate call's lowered arguments (a side
  effect: an `EXHAUSTIVE` CASE inside an aggregate is now analyzed). The
  Fan Trap and Chasm Trap smells in `smells.py` follow the argument's
  columns down through joins; `test_smells.py` shows each total doubled on
  DuckDB.

## Review fixes (2026-11-08)

- An ON equality binds a column only when it pins the column's value
  whatever casts the comparison adds (`equality._pins`): a non-text type
  compared by identity always does; text only against text or a string
  literal (`'01'` and `'1'` both equal 1); a collated, float or unknown type
  never. This was older than this issue: Many To One had it too, and so did
  the FD rules (`a = b`, `a = literal`, outer-join ON), now fixed the same
  way. A text-to-other-type join equality is the new Implicit Cast smell.
- A trap is reported only when the repeated input's key, carried up to the
  aggregate's input, is not a Unique Set there: `WHERE o.id = 1` keeps one
  row per key.
