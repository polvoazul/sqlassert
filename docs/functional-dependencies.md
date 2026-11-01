# Functional Dependencies

Task notes for adding functional-dependency (FD) reasoning to the engine.
Theory first; implementation comes after the theory notes settle.

## Concepts

- **Agree**: two rows agree on a column set X when they have the same value in
  every column of X.
- **Functional dependency `X → Y`**: every pair of rows that agree on X also
  agree on Y, for every possible content of the relation (a structural claim,
  not an observation of data). X and Y are column *sets*: `{x, flag} → y` is
  one FD.
- **Closure `X⁺`**: every column X determines, directly or by chaining FDs.
  Computed by applying known FDs until nothing new is added.
- **Superkey**: a column set that determines every column. **Candidate key**:
  a minimal superkey. sqlassert's Unique Sets are not required to be minimal,
  so they are superkeys; we avoid "candidate key" (see `CONTEXT.md`).
- **Strict vs lax FD**: a strict FD holds on all rows, treating NULL as
  agreeing with NULL (as `GROUP BY` / `DISTINCT` do). A lax FD holds only on
  rows whose determinant is non-null. This mirrors our two uniqueness
  properties: a Unique Set is a lax key, a Non-Null Unique Set is a strict key.

## Theory notes

### Armstrong's axioms

Sound and complete: every implied FD is derivable, and nothing false is.

1. Reflexivity: `Y ⊆ X` ⇒ `X → Y`.
2. Augmentation: `X → Y` ⇒ `XZ → YZ`.
3. Transitivity: `X → Y`, `Y → Z` ⇒ `X → Z`.

Derived: union (`X → Y`, `X → Z` ⇒ `X → YZ`: augment the first with X, the
second with Y, chain), decomposition (`X → YZ` ⇒ `X → Y`). Closure
computation is the efficient way to apply them; the engine never writes proofs
step by step.

### Minimal cover

The smallest FD set equivalent to a given one: single-column right sides, no
redundant FD, no extraneous left-side column. Example: `{a → b, b → c, a → c,
ab → c}` reduces to `{a → b, b → c}` (`ab → c` has `b` extraneous and becomes
`a → c`; `a → c` follows by transitivity). Minimal covers are not unique:
`{a → b, b → a, a → c}` and `{a → b, b → a, b → c}` are equivalent. Relevant
to the engine as a way to keep per-relation FD sets small as they propagate.

### Bag semantics: FDs do not create keys

Textbook: `X⁺ = all columns` ⇒ X is a superkey. That holds for sets only. SQL
relations are bags: two identical rows agree on everything, so no FD rules
them out. Keys still need a distinctness source (`DISTINCT`, `GROUP BY`, a
constraint or declaration, a Unique Join over a keyed input). FDs only
*extend* keys: **X is unique iff `X⁺ ⊇` a known key.**

Corollary: FDs survive row multiplication (a non-unique join duplicates rows
but never makes two rows agree on X and differ on Y); keys do not. FDs are the
more robust thing to propagate.

### Sources of FDs

- Keys: a key determines every column.
- Equality predicates: `a = b` ⇒ `a ↔ b`; `a = constant` ⇒ `∅ → a`.
- Expressions: `a = f(b)` with `f` pure ⇒ `b → a`; `f` also injective ⇒
  `a → b`. Surjectivity is irrelevant; "pure + injective" is the useful class.
- Grouping: the grouping columns determine every output column.

## Propagation through operators

| Operator | Output FDs |
|---|---|
| Filter | Input FDs, plus FDs from the predicate's equality conjuncts (strict: a `WHERE a = b` row has both non-null). |
| Project | Input FDs restricted to surviving columns, computed through closure first (`a → b → c` with `b` dropped still yields `a → c`); plus `inputs → f(inputs)` for pure computed columns, and the reverse for injective ones. |
| Inner join | Union of both sides' FDs, plus `l.x ↔ r.y` for each ON equality. |
| Left join | See below. |
| Aggregate | See below. |

### Left join

- Left-side FDs all survive, even when rows are multiplied.
- `r.y → l.x` (from `ON l.x = r.y`) is only lax: padded rows have `r.y` NULL
  with differing `l.x`.
- `l.x → r.y` is strict only when `l.x` alone decides whether a left row
  matches. In general, `l.x` plus every left column the ON clause references
  determines `r.y` (`ON l.x = r.y AND l.flag` ⇒ `{l.x, l.flag} → r.y`).
- Right-side FD `Y → Z` stays strict when Y contains a column that is NOT NULL
  in the right input: a NULL there implies a padded row, where Z is all NULL
  too. Otherwise an organic NULL in Y breaks it, and it degrades to lax.
- If the join is a Unique Join, a left key determines every column, right side
  included.

### Aggregate (`GROUP BY g`)

- g is the output key, so g determines every output column.
- Input FDs among grouping columns survive: with `g1 → g2`, grouping by
  `g1, g2` has key `{g1}` (the "grouped by `user_id, user_name`" case).
- Aggregates insensitive to row count (`agg(x, …, x) = x`: MIN, MAX,
  ANY_VALUE, AVG) of an input column determined by g act as pass-through of
  that column, so its FDs carry over. Count-sensitive aggregates (SUM, COUNT)
  are new values, determined only by g.
- **GROUP BY elimination**: when the grouping columns contain a Non-Null
  Unique Set of the input, every group has one row. The aggregate behaves like
  a Project: input keys and FDs survive, `SUM(x) = x`, `COUNT(*) = 1`. Requires
  a strict key, because `GROUP BY` collects NULLs into one group.

## Opportunities

Ordered roughly by payoff over cost.

- [ ] **GROUP BY elimination** — no FDs needed: `_preserving_input` lets an
  Aggregate preserve input unique sets when its grouping columns cover an
  input Non-Null Unique Set.
- [ ] **Equivalence classes and constant columns** — group columns that hold
  the same value in every row (a union-find per relation, built from equality
  predicates), and mark a group constant when it equals a literal. Two payoffs,
  both UNKNOWN today:
  - Columns in one group are interchangeable in a key. With `orders` keyed on
    `(user_id, order_no)`, the ON clause groups `o.user_id` with `u.id`, so
    this proves:
    ```sql
    select u.id, o.order_no
    from orders o join users u on o.user_id = u.id
    /**ASSERT UNIQUE(id, order_no)**/
    ```
  - A constant group drops out of a key. With `items` keyed on
    `(tenant_id, id)`, this proves:
    ```sql
    select id from items where tenant_id = 5
    /**ASSERT UNIQUE(id)**/
    ```
  Uniqueness check: map every column to its group's representative on both
  sides, drop constant groups, compare. No closure needed. Needs `Filter` to
  carry its predicate (see TODO.md, IR Limitations). Inner joins and `WHERE`
  only: an outer join's ON equality does not hold on padded rows.
- [ ] **General FDs and closure-based coverage** — `_fds(relation)` beside
  `_unique_sets(relation)`, and `_coverage` checks `closure(S) ⊇ key` instead
  of `S ⊇ key`.
- [ ] **Deterministic expressions** — `inputs → f(inputs)`, with a small
  denylist of impure functions (`random`, `uuid`, `nextval`).
- [ ] **Injective expressions** — a curated allowlist (lossless casts,
  `x ± constant`); everything else non-injective.
- [ ] **Strict/lax FDs in the left-join rules** above.

## Open questions

- **Next to discuss — representation design**: what `_fds(relation)` returns,
  how strict vs lax FDs are stored, and how FD evidence appears in
  `Result.explain()`.
- Representation: equivalence classes + constants only, or general FD sets
  from the start? Unary FDs (`a → b`) fit an N×N boolean matrix whose
  transitive closure (Warshall, or repeated squaring of `I + A`) is the
  closure; multi-column left sides (`ab → c`) are hyperedges and need the
  counter-based closure algorithm (Horn-clause propagation) instead.
- Evidence: how an FD-based proof appears in `Result.explain()`.
- NULL conventions written down once and shared by uniqueness and FD rules.
- Whether these terms (Functional Dependency, Closure, strict/lax) enter
  `CONTEXT.md`.

## References

- Abiteboul, Hull, Vianu, *Foundations of Databases* — dependency theory.
- Paulley, *Exploiting Functional Dependence in Query Optimization* (PhD
  thesis, 2000) — FDs through query operators, NULLs, outer joins, strict vs
  lax.
