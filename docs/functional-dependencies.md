# Functional Dependencies

Task notes for adding functional-dependency (FD) reasoning to the engine.
Theory first; implementation comes after the theory notes settle.

Main source: Paulley, *Exploiting Functional Dependence in Query Optimization*
([PDF](../papers/2000-paulley-exploiting-functional-dependence-in-query-optimization.pdf)).
Citations like "Thm 3, p.96" refer to its printed pages. Background, testing
ideas, and the other papers: [literature-notes.md](literature-notes.md).

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
  rows whose determinant is non-null.
- **Three kinds of key** (Paulley Def 6, Claims 16, 18, 22):
  - *Lax*: no two rows with fully non-null values agree on it. Our Unique Set.
    DuckDB `UNIQUE` is this kind (ANSI: several NULLs allowed).
  - *Null-equal strict*: no two rows agree on it, NULL equal to NULL.
    `DISTINCT`, `GROUP BY`, and `UNION` produce it, even over nullable
    columns. Not yet a property of ours.
  - *Non-null strict*: null-equal strict with every column non-null. Our
    Non-Null Unique Set.
- **Lax rules need non-null middles** (Paulley fd4b, fd7b, eq9b; counterexample
  Fig. 2.1, p.38). Lax transitivity and lax decomposition are sound only when
  the chained or dropped columns are non-null. A lax FD or equivalence becomes
  strict once every column in it is known non-null (fd6, eq7).

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

### Inclusion dependencies

`R[X] ⊆ S[Y]`: every X value of R appears as a Y value of S. A foreign key is
an inclusion dependency whose right side is a key. Theory behind issues
[19–22](issues/README.md) (Foreign Keys, `EXACTLY ONE`):

- Unique Join = at most one match (right key). Inclusion dependency = at least
  one. Together: exactly one; the join neither drops nor duplicates left rows.
- `o LEFT JOIN u ON o.user_id = u.id` with FK `orders[user_id] ⊆ users[id]`:
  when `o.user_id` is non-null the row is matched, so `u.id = o.user_id` and
  every NOT NULL column of `u` is non-null (nullable `u` columns can still be
  NULL organically). When `o.user_id` is NULL the row is padded: SQL foreign
  keys (`MATCH SIMPLE`) never require NULLs to match.
- With `o.user_id` NOT NULL as well, the LEFT JOIN behaves as an INNER JOIN.
- Caveat: many warehouses do not enforce foreign keys; the same trust question
  as unenforced keys.

## Propagation through operators

| Operator | Output FDs |
|---|---|
| Filter | Input FDs, plus FDs from the predicate's equality conjuncts (strict: a `WHERE a = b` row has both non-null). See below. |
| Project | Input FDs restricted to surviving columns, computed through closure first (`a → b → c` with `b` dropped still yields `a → c`); plus `inputs → f(inputs)` for pure computed columns, and the reverse for injective ones. |
| Inner join | Union of both sides' FDs, plus `l.x ↔ r.y` for each ON equality. |
| Left join | See below. |
| Full join | Input FDs survive only with η (below) on their side; one-sided ON conditions give nothing; cross-side FDs need η on both sides (Paulley Lemma 10, Thm 4, p.100). |
| Aggregate | See below. |
| Set operations | See below. |

### Filter (Paulley Claim 20, pp.77–79)

A `WHERE` keeps only rows where the predicate is true ("false-interpreted":
unknown counts as false), so its equalities are strict.

- `col = col`: strict equivalence, FDs both ways, both columns non-null.
- `col = constant`: the column is constant (`∅ → col`) and non-null.
- `col IS NOT NULL`: non-null. `col IS NULL`: equal to a NULL constant.
- `LIKE` without wildcards is an equality. `= (uncorrelated scalar subquery)`
  is an unknown but single constant.
- Disjunctions are ignored. Inside `EXISTS`, correlated outer columns are
  constants (§3.2).
- Non-nullness spreads across strict equivalences, which can turn lax FDs
  strict ("Set definite", p.123).
- Conditions that hold where the predicate is *not false* (CHECK
  constraints: "true-interpreted") give only lax facts.

### Left join

Paulley Thm 3, p.96. Define **η(p, X)**: some column x ∈ X either is NOT NULL
in the right input, or makes the ON condition p false or unknown when x is
NULL (Def 37, Lemma 7). Either way, a NULL in x means a padded row.

- Left-side FDs and equivalences all survive, even when rows are multiplied.
- Right-side FD `X → Y` stays strict when η(p, X) holds: a NULL in X then
  implies a padded row, where Y is all NULL too. Otherwise an organic NULL in X
  breaks it, and it degrades to lax. The second case of η covers the common
  one: in `ON l.x = r.y`, a NULL `r.y` fails the condition, so FDs from `r.y`
  stay strict even when `r.y` is nullable. Also strict when `X = Y` is a strict
  equivalence on the right side (Cor 2). A lax right-side FD turns strict when
  every column in it satisfies η (Lemma 8).
- FDs from the ON condition (Lemma 9, p.93):
  - Conditions only on the left side are dropped.
  - Equalities among right-side columns stay strict, as do their
    equivalences.
  - *All* left columns referenced in p determine a right column Y bound by p,
    when η(p, Y): `ON l.x = r.y AND l.flag` ⇒ `{l.x, l.flag} → r.y`; right-side
    constants count too: `ON l.a = r.b AND r.d = 2` ⇒ `{l.a} → r.d`
    (Example 20).
  - Everything else is lax, including the equivalence `l.x ≃ r.y`.
    `r.y → l.x` is only lax: padded rows have `r.y` NULL with differing `l.x`.
    So union-find may not merge `l.x` and `r.y`.
- If the join is a Unique Join, a left key determines every column, right side
  included.
- Idea: separate padding NULLs from organic NULLs. Give each outer join a
  virtual `matched` column. Every right-side column that is NOT NULL in the
  right input is NULL exactly when `matched` is false, so those columns are
  NULL together (Paulley's null constraint, Def 38). A null-rejecting
  predicate on any one of them (`WHERE u.id IS NOT NULL`, `u.id = 5`) forces
  `matched`, which makes them all non-null, turns lax FDs strict, and lets the
  LEFT JOIN be treated as INNER. Organic NULLs in nullable right columns stay
  untracked.

### Aggregate (`GROUP BY g`)

- g is the output key, so g determines every output column.
- Input FDs among grouping columns survive: with `g1 → g2`, grouping by
  `g1, g2` has key `{g1}` (the "grouped by `user_id, user_name`" case).
- Aggregates insensitive to row count (`agg(x, …, x) = x`: MIN, MAX,
  ANY_VALUE, AVG) of an input column determined by g act as pass-through of
  that column, so its FDs carry over. Count-sensitive aggregates (SUM, COUNT)
  are new values, determined only by g. The pass-through rule is our
  extension: Paulley (Claims 22–23, p.131) treats every aggregate as a new
  value. Sound only when `g → x` is strict. `COUNT` is non-null; every other
  aggregate is nullable.
- **GROUP BY elimination**: when the grouping columns contain a strict key of
  the input, every group has one row. The aggregate behaves like a Project:
  input keys and FDs survive, `SUM(x) = x`, `COUNT(*) = 1`. A lax key (Unique
  Set) is not enough, because `GROUP BY` collects NULLs into one group. A
  null-equal strict key is (no two rows agree on it, NULL equal to NULL): a
  Non-Null Unique Set, or the output of `DISTINCT` / `GROUP BY` even over
  nullable columns (see literature-notes.md).

### Set operations (Paulley §3.2.5–3.2.7)

- `INTERSECT` (and `ALL`): FDs and keys of both inputs hold; corresponding
  columns are strictly equivalent. A lax FD turns strict when either side
  makes its columns non-null (Claim 21, Lemma 6, Cor 1).
- `EXCEPT` (and `ALL`): everything that holds in the left input holds.
- `UNION ALL`: no FDs in general (proving the branches disjoint is
  undecidable); only equivalences and constants shared by both branches
  survive (Example 15).
- `UNION` (distinct): all output columns form a null-equal strict key.
- Our extension: branches tagged with distinct literals (`select 'a' as tag,
  … union all select 'b', …`) make `(tag, branch key)` a key.

### Window functions

A window adds columns without adding or removing rows, so it behaves like a
Project for structure: the row identity is inherited, and every input key and
FD survives. The new columns:

- `row_number() over (partition by g …)`: `{g, rn}` is a strict key (row
  numbers are distinct within a partition; NULLs in g share one partition).
  `QUALIFY rn = 1` then makes `g` a key (the current special case).
- `rank()` / `dense_rank()`: ties repeat, so no key.
- A window aggregate without `ORDER BY` (`sum(x) over (partition by g)`) is
  constant per partition: `g → sum_g`. With `ORDER BY` (running totals) it
  varies within a partition: no FD.
- Window values depend on the whole partition, so windows are not monotone
  (adding rows changes existing outputs); the two-row oracle argument does not
  cover them.

## Opportunities

Ordered roughly by payoff over cost.

- [ ] **Null-equal strict key property** — what `DISTINCT`, `GROUP BY`, and
  `UNION` produce over nullable columns; needed by GROUP BY elimination and
  set-operation rules.
- [ ] **GROUP BY elimination** — no FDs needed: `_preserving_input` lets an
  Aggregate preserve input unique sets when its grouping columns cover a
  strict key of the input (a Non-Null Unique Set, or a `DISTINCT` / `GROUP BY`
  output).
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
  carry its predicate (see [issue 23](issues/0023-close-ir-lowering-gaps.md)). Merge **strict**
  equivalences only (inner-join ON, `WHERE`): lax ones are not transitive
  (Paulley eq9b), and an outer join's ON equality does not hold on padded rows.
- [ ] **General FDs and closure-based coverage** — `_fds(relation)` beside
  `_unique_sets(relation)`, and `_coverage` checks `closure(S) ⊇ key` instead
  of `S ⊇ key`.
- [ ] **Deterministic expressions** — `inputs → f(inputs)`, with a small
  denylist of impure functions (`random`, `uuid`, `nextval`).
- [ ] **Injective expressions** — a curated allowlist (lossless casts,
  `x ± constant`); everything else non-injective.
- [ ] **Strict/lax FDs in the left-join rules** above, including η.
- [ ] **Set-operation rules** above (INTERSECT, EXCEPT, UNION, tagged
  UNION ALL).
- [ ] **Window functions** — lower windows into the IR (today only
  `QUALIFY row_number() … = 1` is recognized), inherit input keys and FDs, and
  add the `row_number` key and partition-constant aggregate FDs above.

## Open questions

- **Next to discuss — representation design**: what `_fds(relation)` returns,
  how strict vs lax FDs are stored, and how FD evidence appears in
  `Result.explain()`.
- Representation: equivalence classes + constants only, or general FD sets
  from the start? Unary FDs (`a → b`) fit an N×N boolean matrix whose
  transitive closure (Warshall, or repeated squaring of `I + A`) is the
  closure; multi-column left sides (`ab → c`) are hyperedges and need the
  counter-based closure algorithm (Horn-clause propagation) instead.
- **Keys as FDs to a row id** (Paulley Def 27, §3.1.2): give each relation a
  virtual column ι and write a key K as `K → ι`. Uniqueness becomes "does
  `closure(S)` reach ι?", one test instead of key sets plus FDs. Joins give
  `ι(L) ∪ ι(R) → ι(out)` (Claim 19); a Unique Join is `ι(L) → ι(out)`; an
  input with at most one row lets the other side's ι determine everything
  (p.254).
- **FD-graph** (Paulley, after Ausiello–D'Atri–Saccà): a hypergraph with
  compound vertices for multi-column left sides; closure is BFS with counters,
  O(V²) (§3.6.1, Lemma 29); constants seed it; the lax variant steps only
  through non-null columns. Shipped in Sybase SQL Anywhere.
- **Hide, don't delete**: Paulley marks projected-away columns instead of
  removing them, so transitive FDs survive without recomputing closure
  (§3.2.2). Our IR already keeps each stage's input reachable.
- Evidence: how an FD-based proof appears in `Result.explain()`.
- NULL conventions written down once and shared by uniqueness and FD rules.
- Whether these terms (Functional Dependency, Closure, strict/lax) enter
  `CONTEXT.md`.

## References

- Abiteboul, Hull, Vianu, *Foundations of Databases* — dependency theory.
- Paulley, *Exploiting Functional Dependence in Query Optimization* (PhD
  thesis, 2000) — FDs through query operators, NULLs, outer joins, strict vs
  lax. [PDF](../papers/2000-paulley-exploiting-functional-dependence-in-query-optimization.pdf).
- [literature-notes.md](literature-notes.md) — how the optimizer literature
  differs from sqlassert, testing ideas, links to code, and the other papers
  (Kossmann 2022, Liu 2023, Lindner 2024).
