# Functional Dependencies

Functional-dependency (FD) reasoning in the engine: the theory, then how the
rules implement it (Representation).

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
- Padding NULLs and organic NULLs are tracked apart: see **Null sources**
  in the representation design (Paulley's null constraint, Def 38, is the
  single-join case).

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
  `UNION` produce over nullable columns. The closure already computes it
  (`definite = ∅`); it needs a name and a marker.
- [x] **GROUP BY elimination** — falls out of closure: an Aggregate's
  AnyAggregate column carries its input's dependencies when the grouping
  columns strictly determine it, and two groups agreeing on a key of the input
  are one group.
- [x] **Equalities and constants as FDs** — `WHERE` and inner-join `ON`
  equalities give `a ↔ b`, `a = literal` and `a IS NULL` give `∅ → a`:
  `select id from items where tenant_id = 5` is unique on `id`, and
  `u.id` stands in for `o.user_id` under `on o.user_id = u.id`.
- [x] **General FDs and closure-based coverage** — see Representation below.
- [ ] **Deterministic expressions** — `inputs → f(inputs)`, with a small
  denylist of impure functions (`random`, `uuid`, `nextval`).
- [ ] **Injective expressions** — a curated allowlist (lossless casts,
  `x ± constant`); everything else non-injective.
- [x] **Strict/lax FDs in the left-join rules** above, including η.
- [x] **Set-operation rules** — UNION / INTERSECT / EXCEPT without `ALL` key
  all their columns; INTERSECT and EXCEPT keep the left input's dependencies.
  Not yet: tagged `UNION ALL`, and the right arm's columns.
- [ ] **Window functions** — lower windows into the IR (today only
  `QUALIFY row_number() … = 1` is recognized), inherit input keys and FDs, and
  add the `row_number` key and partition-constant aggregate FDs above.

## Representation

The code is `sqlassert/rules/uniqueness.py` and `sqlassert/rules/nullability.py`.

### Vocabulary in the rules

```python
@dataclass(frozen=True)
class RowIdentity:          # Paulley's ι: one per relation, never in the IR
    relation: ir.RelationExpr

@dataclass(frozen=True)
class Dependency:
    determinant: frozenset[ir.OutputColumn | RowIdentity]   # empty = constant
    dependent: ir.OutputColumn | RowIdentity
    strict: bool                                            # False = lax

@analysis(Stratum.FUNCTIONAL_DEPENDENCY)
def _functional_dependencies(relation) -> Result[dict[Dependency, Result[bool]]]: ...

@analysis(Stratum.CLOSURE)
def _closure(relation, columns, definite) -> Result[Closure]: ...
```

(`FunctionalDependency` is the user-facing Property; the internal value is
`Dependency`.)

- **One mechanism for uniqueness.** Keys are FDs to `RowIdentity`;
  equalities are FD pairs; constants are FDs from `∅`. A column set is a
  Unique Set when its closure reaches the relation's Row Identity, and a join
  is a Unique Join when the right columns the ON condition binds reach the
  right input's. Union-find serves value facts only ([Equality groups](#equality-groups)); it
  never decides uniqueness, because it covers equalities and constants only
  and forgets which equality merged two columns, which `explain()` needs.
- **Strict and lax in one closure.** `_closure(relation, columns, definite)`
  considers the pairs of rows on which every `definite` attribute is non-null.
  A Unique Set and a `DETERMINES` assertion pass `definite = columns` (lax: a
  NULL exempts a row); `definite = ∅` is the strict, null-equal closure, used
  for what an Aggregate's groups share. Strict dependencies always fire; a lax
  one fires only when every determinant column is definite: requested, or
  proved non-null in the relation (Paulley fd4b, fd6, fd7b).
- **Delegation instead of copying ("hide, don't delete").** A relation's
  `_functional_dependencies` holds only what it introduces itself, over its own
  output columns and Row Identity (a join also names its inputs' identities).
  `_closure` maps the attributes it has reached into each input through
  pass-through columns, takes the input's closure there, and maps what that
  reaches back. Columns a projection drops still take part, transitively, with
  no recomputation. This deviates from the draft, which copied input
  dependencies up as hidden columns: copying breaks on self joins (two Aliases
  of one Named Relation would share its columns and identity) and on outer
  joins (a hidden padded-side column has no value on a padded row); delegation
  avoids both because every closure is about one relation's rows. How rows map
  onto input rows decides when delegating is sound:
  - Alias, Named Relation, Filter, Project, QUALIFY: one output row is one
    input row; `RowIdentity(out) ↔ RowIdentity(in)`.
  - Join: each side separately. A side the join pads is compared as input rows
    only when the two rows cannot be one matched and one padded: some reached
    column of that side is NULL only by padding (η), is requested non-null, or
    the side's identity is reached; or the preserved side's ON columns are
    reached, which fixes whether both rows matched.
  - Aggregate, Distinct, INTERSECT / EXCEPT: an output row stands for input
    rows sharing its pass-through values. Two output rows agreeing on some of
    them have representatives agreeing on them, so what those determine in the
    input carries back to pass-through outputs, to an AnyAggregate column whose
    source is constant per group, and to the output's Row Identity (one input
    row belongs to one output row).
- **Evidence.** `_closure` returns a `Closure` whose `members` hold one
  `Result` per reached attribute, citing the dependency or input closure that
  reached it; when several reach it, their evidence is combined, so two
  covering keys both appear in `explain()`. An UNKNOWN names, for each known
  key down through the inputs the closure looked into, its missing columns.

### Where dependencies come from

- Knowledge and declarations: a Unique Set is a lax `columns → RowIdentity`;
  a declared `DETERMINES` is lax `determinant → each dependent`.
- Literal outputs: `∅ → c`.
- `WHERE` and inner-join `ON`: `a = b` both ways, `a = literal` and
  `a IS NULL` from `∅`.
- Join: `{RowIdentity(L), RowIdentity(R)} → RowIdentity(out)`; a Unique Join
  adds `RowIdentity(L) → RowIdentity(out)` and `RowIdentity(L) →
  RowIdentity(R)`. An Exactly One Join needs nothing more: its
  `RowIdentity(out) ↔ RowIdentity(L)` is this plus "an identity determines
  every column".
- Outer join ON (Paulley Lemma 9): equalities within the padded side stay
  strict; the preserved side's ON columns determine every padded column the
  condition binds to them or to a literal (`{t.x, t.y, t.z} → s.d` in Example
  20); a cross-side equality is lax from the padded side. With an
  unrecognized conjunct, only the preserved row's identity fixes the match.
- GROUP BY, DISTINCT, QUALIFY, and distinct set operations: their key columns
  → `RowIdentity` (strict, NULLs grouped together).
- A Row Identity determines every column of its relation.

### Null sources

`_null_sources(column)` returns the set of reasons a column can be NULL:

- `Organic`: the value itself may be NULL (nullable source column, opaque
  expression, NULL literal). Per column.
- `Padding(J)`: outer join J pads the column. Tagged with the Aliases it was
  seen through, so two occurrences of one relation never share a padding.

Non-null means the set is empty, so `NonNullColumn` keeps its meaning.

- **Padding promotion.** A null-rejecting `WHERE` or inner-join `ON` conjunct
  (`IS NOT NULL`, `= ...`) on a column carrying `Padding(J)` proves J matched,
  and removes `Padding(J)` from every column carrying it.
- **Organic removal** is per column: the same conjunct removes `Organic` from
  the column it constrains only.
- **The ON condition** acts on the padded side's rows before padding: a
  padded-side column it rejects loses `Organic`, leaving only `Padding(J)`.
  That is η: padding is the column's only NULL source.
- A proved Exactly One LEFT join pads nothing.
- An Aggregate's non-grouping columns turn padding into `Organic`: the row a
  group's value came from is not tied to the rows the join padded.

Explanations name each source: "nickname is nullable in users", "nickname is
padded by the LEFT JOIN".

### Equality groups

`sqlassert/rules/equality.py`. `_equality_groups(relation)` partitions a
relation's output columns into groups that carry one value on every row
(`EqualityGroups.groups`, each in output order); `_equal_columns(column)` is
the group of one column, the question Enumerated Domains and Foreign Key
propagation ask. Each group's evidence cites every equality that merged it,
with its line.

- A union-find over **strict** equalities only: `WHERE` and inner-join `ON`
  conjuncts `a = b` between columns. An outer join's ON never merges, not
  even within its padded side; neither does `a = f(b)`, a disjunction, or a
  semi or anti join's ON.
- Input groups pass through every column that passes an input column
  through: Alias, Named Relation, Filter, Project, both sides of a join,
  grouping columns, DISTINCT, QUALIFY, and the left input of INTERSECT and
  EXCEPT. A computed column (an aggregate included) starts its own group;
  UNION passes nothing. Groups are closed at each relation, so a column a
  projection drops still links the columns it was equal to.
- On a row an outer join padded, a group of the padded side is NULL in every
  member: value-equal, NULL agreeing with NULL. Consumers read non-null
  values (Enumerated Domains, `MATCH SIMPLE` Foreign Keys), so this is enough.
- Not yet: constants (`a = literal` merges nothing; Enumerated Domains read
  such conjuncts directly), and the ON of a proved Exactly One LEFT join,
  which pads nothing.

### Kept outside the FD closure

- **Value equality.** `a = b` is stronger than the FD pair `a ↔ b`. Facts
  about values (a constant's value, ENUM domains, inclusion dependencies) need
  the equality itself: [equality groups](#equality-groups).
- **Inclusion dependencies / foreign keys**: a relation between two relations,
  not a column fact (Referential Coverage, Foreign Key).

### Strata

The memoized engine cannot compute mutually recursive facts. The IR's
relations form a DAG, so a cycle can only happen between facts about the
**same** relation. Facts about one relation are therefore ordered: an analysis
about R may use any fact about R's inputs, but facts about R only at its own or
a lower stratum. `Stratum` in `python_engine.py` encodes it, its docstring is
the canonical statement, and `@analysis` asserts it on every call:

1. Equality groups (only the relation's own conjuncts and its inputs' groups).
2. Unique join, exactly-one join.
3. Non-null (null sources; exactly-one left joins pad nothing).
4. Functional dependencies (η uses null sources; joins use Unique Joins).
5. Closure, and `DETERMINES` assertions.
6. Unique sets.
7. Foreign keys.
8. Enumerated domains (equality groups, then each member's own domain).

Predicate extraction needs no stratum: it is a plain helper over the IR's
conjuncts. Two exceptions to plan for:

- **Correlated subqueries** make the inner relation depend on the outer one
  and vice versa, a cycle across relations. Lower them as a dependent join
  (apply), so the outer input sits below the subquery and the DAG holds
  ([issue 23](issues/0023-close-ir-lowering-gaps.md)).
- **Recursive CTEs** are a genuine fixpoint. They stay opaque; supporting them
  needs bespoke fixpoint machinery, not strata.

Self joins are fine: each side is its own `Alias`, and closure delegation is
about one relation's rows at a time.

### User-facing

- **`DETERMINES` markers**: `/**ASSERT DETERMINES(a, b -> c)**/` asks for a
  proof; `/**DECLARE DETERMINES(zip -> city)**/` supplies a business-logic FD
  no schema states, as Oracle dimensions did. The Property is **lax**, like a
  Unique Set: rows with a NULL determinant are exempt, and dependents compare
  NULL as equal to NULL. A strict (null-equal) form would need its own name
  and marker.
- **`RelationFacts` reports minimal keys only.** `suggest` already reports
  minimal sets, and a superset of a reported key is unique anyway
  (`is_unique` answers for it). Keys are enumerated smallest first up to three
  columns; when none is that small, one minimal key found by shrinking all the
  columns is reported, so a relation whose only keys are wider than three
  columns lists one of them.
- **Assertions from optimizer preconditions** (literature-notes.md): redundant
  `DISTINCT`, redundant `GROUP BY` columns, join elimination, single-valued
  scalar subquery. Each is one closure query.
- Someday/maybe: **optimization hints** from proved dependencies (Lindner et
  al., [`references.md`](references.md)): report a redundant `GROUP BY` column
  when another grouping column determines it, and a join whose right side is
  unique and contributes no output columns ("join only filters", a semi-join
  candidate).

## Open questions

- Single-row relations: an aggregate without `GROUP BY` is lowered as a
  Project today, so it gets no `∅ → RowIdentity`; a `WHERE` that pins a key to
  constants already does.
- Lax steps through equalities: a column equal to a definite column is
  definite on the compared rows, which closure does not use yet.
- `IS NOT DISTINCT FROM`, ranges, `LIKE`, and `OR` are kept as opaque
  conjuncts: they never bind a column, and ranges and `LIKE` do not yet reject
  NULL either.

## References

- Abiteboul, Hull, Vianu, *Foundations of Databases* — dependency theory.
- Paulley, *Exploiting Functional Dependence in Query Optimization* (PhD
  thesis, 2000) — FDs through query operators, NULLs, outer joins, strict vs
  lax. [PDF](../papers/2000-paulley-exploiting-functional-dependence-in-query-optimization.pdf).
- [literature-notes.md](literature-notes.md) — how the optimizer literature
  differs from sqlassert, testing ideas, links to code, and the other papers
  (Kossmann 2022, Liu 2023, Lindner 2024).
