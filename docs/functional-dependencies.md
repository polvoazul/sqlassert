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
- Composite: `ON l.a = r.x AND l.b = r.y` needs `L[a, b] ⊆ R[x, y]`, the
  tuple; `L[a] ⊆ R[x]` and `L[b] ⊆ R[y]` may be met by different rows. Every
  ON conjunct must be such an equality (another may reject the covering row),
  and `a`, `b` non-null (`MATCH SIMPLE` exempts a row with any NULL component).
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

## Paulley audit (issue 35)

Every rule of Paulley 2000 §2.5.2 and §3.1–3.2, checked against
`sqlassert/rules/`. Rows are audited in order and committed in batches, so
the first `pending` row is where a paused audit resumes. Status:
**implemented** (the code, and a test that exercises it), **different** (we
deliberately do otherwise; why), **missing** (filed where), or **n/a** (not
an SQL construct we analyze). ι is a relation's Row Identity
(`RowIdentity`), η(p, X) is Def 37, a lax FD is one our closure steps
through only with definite determinants.

| ID | Paulley rule | Where | Status | Our code | Test |
|---|---|---|---|---|---|
| A1 | Reflexivity: Y ⊆ X ⇒ X → Y | fd1, Lemma 1, p.38 | implemented | `uniqueness._closure` (single-column dependents, Def 31) | `test_paulley_audit.py` A1 |
| A2 | Strict augmentation: X → Y ⇒ XW → YZ (Z ⊆ W) | fd2a, p.38 | implemented | `uniqueness._closure` (single-column dependents, Def 31) | `test_paulley_audit.py` A2 |
| A3 | Lax augmentation | fd2b, p.38 | implemented | `uniqueness._closure`; a lax step needs a definite determinant | `test_paulley_audit.py` A3 |
| A4 | Strict union: X → Y, X → Z ⇒ X → YZ | fd3a, p.38 | implemented | `uniqueness._closure` (single-column dependents, Def 31) | `test_paulley_audit.py` A4 |
| A5 | Lax union | fd3b, p.38 | implemented | `uniqueness._closure`; a lax step needs a definite determinant | `test_paulley_audit.py` A5 |
| A6 | Strict decomposition | fd4a, p.38 | implemented | `uniqueness._closure` (single-column dependents, Def 31) | `test_paulley_audit.py` A6 |
| A7 | Lax decomposition only with Y definite | fd4b, Lemma 3, p.39 | different | a lax `Dependency` fires only from a definite determinant (`_closure.fire`), and every lax FD we derive is lax in the determinant only, its dependent equal: unique keys onto the Row Identity, declared DETERMINES, a padded column onto the preserved one. Stronger than Paulley's lax FD and still sound, so decomposition needs no definite dependent | `test_paulley_audit.py` A7 |
| A8 | Weakening: strict ⇒ lax | fd5, p.38 | implemented | `uniqueness._closure` (single-column dependents, Def 31) | `test_paulley_audit.py` A8 |
| A9 | Strengthening: lax ⇒ strict when XY definite | fd6, p.38 | implemented | `uniqueness._closure`; a lax step needs a definite determinant | `test_paulley_audit.py` A9 |
| A10 | Strict transitivity | fd7a, p.38 | implemented | `uniqueness._closure` (single-column dependents, Def 31) | `test_paulley_audit.py` A10 |
| A11 | Lax transitivity only through a definite middle | fd7b, Lemma 2, p.38 | implemented | `uniqueness._closure`: a lax step fires only from definite attributes | `test_paulley_audit.py` A11, A11- |
| E1 | Strict equivalence: identity, commutativity, transitivity | eq1–eq3, Claim 14, p.69 | implemented | `equality._equality_groups` (union-find), `uniqueness._equality_dependencies` | `test_paulley_audit.py` E1 |
| E2 | Strict equivalence implies FDs both ways | eq4, p.69 | implemented | `uniqueness._equality_dependencies` (both directions) | `test_paulley_audit.py` E2 |
| E3 | Lax equivalence: commutativity; strict ⇒ lax | eq5–eq6, Lemma 4, p.69 | different | no lax equivalence object: an outer join's cross equality gives the lax FD padded → preserved and a strict FD from the match status (`uniqueness._outer_on_dependencies`); equality groups hold strict equivalences only | `test_paulley_audit.py` E5 |
| E4 | Lax equivalence strengthened when both definite | eq7, p.69 | missing | equality groups never strengthen: `WHERE r.z IS NOT NULL` after `LEFT JOIN r ON r.z = l.a` does not put z and a in one group, so domains and Foreign Key stand-ins do not cross (the closure does, via A9). Filed in issue 23 |  |
| E5 | Lax equivalence implies lax FDs both ways | eq8, p.69 | implemented | `uniqueness._outer_on_dependencies` (lax padded → preserved) | `test_paulley_audit.py` E5 |
| E6 | Lax equivalence transitive: strict∘lax, or lax∘lax through a definite middle | eq9a/b, Lemma 5, p.70 | implemented | `uniqueness._closure` (strict after lax; lax after lax only through a definite middle) | `test_paulley_audit.py` E6, E6- |
| S1 | PRIMARY KEY and unique index: strict key K → ι | Claim 16, p.73 | different | PRIMARY KEY: strict key (`NonNullColumn` + `UniqueSet` Knowledge, `convert._table_knowledge`). A unique index is a lax key like UNIQUE: SQL unique indexes repeat NULLs, Paulley's strict reading would be unsound | `test_paulley_audit.py` S1, S1- |
| S2 | UNIQUE constraint: lax key U ↦ ι | Claim 16, p.73 | implemented | `convert._table_knowledge` → `UniqueSet`, a lax key onto the Row Identity (`uniqueness._functional_dependencies`) | `test_paulley_audit.py` S2, S2- |
| S3 | Equality to a constant: ∅ → A | §3.1.3, p.68 | implemented | `uniqueness._equality_dependencies` (`∅ → col` for `col = literal`) | `test_paulley_audit.py` S3 |
| S4 | Scalar function: X → λ(X), result possibly NULL | §3.1.4, p.71 | different | `OpaqueExpression.inputs` and `uniqueness._computed`: only built-in deterministic functions, and only of inputs compared by identity (NOCASE text and DOUBLE equal values a function tells apart) | `test_paulley_audit.py` S4 |
| S5 | CHECK constraints: equalities over non-null columns strict, else lax | §3.2.1, p.73 | missing | CHECK constraints are ignored without a diagnostic. `CHECK (m = 1)` or `CHECK (m IN (...))` would give a domain and a constant (true-interpreted: NULL passes, so lax). Filed in issue 23 |  |
| P1 | Projection keeps input FDs, transitively through dropped columns | Claim 17, p.74 | implemented | `uniqueness._row_preserving` delegation: closure runs in the input, so dropped columns still connect (hide, don't delete) | `test_paulley_audit.py` P1 |
| P2 | Projection: X → λ(X) and ι → λ(X) | Claim 17, p.74 | implemented | `uniqueness._computed` on the Project delegation (with S4's restriction) | `test_paulley_audit.py` P2 |
| P3 | Distinct projection: selected attributes → ι | Claim 18, p.75 | implemented | `uniqueness._functional_dependencies` (Distinct: output columns → ι) | `test_paulley_audit.py` P3 |
| C1 | Cartesian product keeps both inputs' FDs and equivalences | Claim 19, p.75 | implemented | `uniqueness._join_side` delegations for `cross` | `test_paulley_audit.py` C1 |
| C2 | Cartesian product: ι(S) ∪ ι(T) ↔ ι | Claim 19, p.76 | implemented | `uniqueness._join_dependencies` (`(ι(left), ι(right)) → ι`) | `test_paulley_audit.py` C2, C2- |
| C3 | An input with at most one row: the other's key is the result's key | §3.2.3, p.75 (omitted from his algorithm) | implemented | `unique_join` with no conjuncts: the right input's empty set reaches its Row Identity; a scalar aggregate is grouped by nothing (issue 23) | `test_paulley_audit.py` C3 |
| R1 | Restriction keeps strict FDs and strict equivalences | Claim 20, p.78 | implemented | `uniqueness._row_preserving` (Filter delegates to its input) | `test_paulley_audit.py` R1 |
| R2 | Restriction strengthens a lax FD whose columns become definite | Claim 20, p.78 | implemented | `uniqueness._definite` reads `non_null_column` of the Filter's output, so a lax step through a column the WHERE makes non-null fires | `test_paulley_audit.py` R2 |
| R3 | Restriction strengthens a lax equivalence whose columns become definite | Claim 20, p.78 | missing | see E4 |  |
| R4 | Function in WHERE: X → λ(X) | Claim 20, p.78 | missing | `WHERE b = a + 1` gives nothing: `_equality_dependencies` reads column-to-column and column-to-literal equalities only, not a deterministic expression's inputs. Filed in issue 23 |  |
| R5 | False-interpreted Type 1/2 equality: strict equivalence, FDs both ways | Claim 20, p.79 | implemented | `uniqueness._equality_dependencies`, `equality._equality_groups`, `nullability._null_rejected` | `test_paulley_audit.py` R5 |
| R6 | True-interpreted equality (correlation inside a subquery): lax equivalence and FDs | Claim 20, p.79 | different | EXISTS is lowered as a semi join whose ON is false-interpreted, which is exact (issue 23); the true-interpreted forms (NOT IN, ALL) are not lowered, so no lax facts arise |  |
| R7 | IS NOT NULL: attribute definite | §3.2.4, p.77 | implemented | `nullability._null_rejected` (IS NOT NULL) | `test_paulley_audit.py` R7 |
| R8 | IS NULL: equality with a NULL constant | §3.2.4, p.77 | implemented | `uniqueness._equality_dependencies` (`col IS NULL`: `∅ → col`) | `test_paulley_audit.py` R8 |
| R9 | LIKE without wildcards is an equality | §3.2.4, p.77 | missing | LIKE is opaque; without wildcards it is an equality. Filed in issue 23 |  |
| R10 | Equality with an uncorrelated scalar subquery: an unknown constant | §3.2.4, p.77 | missing | `col = (uncorrelated scalar subquery)` gives nothing; the subquery is one value for every row, so `∅ → col`. Filed in issue 23 |  |
| R11 | Null-intolerant conjunct makes its attributes, and those strictly equal to them, definite | §3.2.4, p.78 | missing | partly: `=`, `IN` and `IS NOT NULL` reject NULL (`nullability._null_rejected`) and non-nullness spreads through Equality Groups, but other comparisons (`<`, `>`, `<>`, `BETWEEN`, `LIKE`), which Paulley counts as null-intolerant, are opaque. Filed in issue 23 | `test_paulley_audit.py` R11 |
| R12 | EXISTS as restriction; correlation attributes are constants of the subquery | §3.2, p.72; §2.3.1.2 | implemented | WHERE EXISTS / IN as semi joins (`convert._subquery_join`); outer columns resolve in the ON | `test_paulley_audit.py` R12 |
| R13 | Disjunctions give nothing | §3.2.4, p.77 | implemented | OR is an opaque conjunct (`convert._lower_conjuncts`) | `test_paulley_audit.py` R13- |
| I1 | Intersection keeps strict FDs of either input | Lemma 6, Claim 21, p.79 | missing | partly: the left input's FDs hold (`uniqueness._delegations`, `_grouped` onto the left), the right input's do not. Sound only under identity comparison (an INTERSECT row carries the left value). Filed in issue 23 | `test_paulley_audit.py` I1 |
| I2 | Intersection keeps superkeys of either input | Corollary 1, p.80 | missing | partly, as I1: the left input's keys hold, the right input's do not. Filed in issue 23 | `test_paulley_audit.py` I2 |
| I3 | Intersection strengthens lax FDs and equivalences made definite | Claim 21, p.81 | implemented | nullability through INTERSECT takes either input (`nullability._set_operation_sources`), and the closure then strengthens lax steps (A9) | `test_paulley_audit.py` I3 |
| I4 | Intersection: corresponding attributes strictly equivalent | Claim 21, p.81 | different | an output column refers to the left input; `SetOperation.arms` pairs the right one by position, used for nullability and domains (domains only under identity comparison for strings) |  |
| I5 | Intersection: ι(S) ↔ ι(T) | Claim 21, p.81 | n/a | the Row Identity of an INTERSECT is reached through the left input only; the right input's identity is never needed |  |
| U1 | UNION ALL keeps no input FDs | §3.2.6, p.81 | implemented | `uniqueness._delegations` gives a UNION no delegation | `test_paulley_audit.py` U1- |
| U2 | UNION ALL keeps strict equivalences (and constants) holding in both inputs | §3.2.6, Example 15, p.82 | missing | equivalences and constants holding in both UNION ALL inputs are dropped (constants survive only as Enumerated Domains). Filed in issue 23 |  |
| U3 | Distinct UNION: all attributes form a superkey | §3.2.6, p.82 | implemented | `uniqueness._functional_dependencies` (`SetOperation(distinct=True)`: output columns → ι) | `test_paulley_audit.py` U3 |
| D1 | Difference keeps every FD, equivalence and superkey of the left input | §3.2.7, p.82 | implemented | `uniqueness._delegations` (`EXCEPT` delegates to its left input) | `test_paulley_audit.py` D1 |
| G1 | Partition keeps input FDs and equivalences | Claim 22, p.83 | implemented | `uniqueness._grouped` delegation onto the input, from the grouping columns | `test_paulley_audit.py` G1 |
| G2 | Grouping function: X → λ(X) | Claim 22, p.83 | implemented | `uniqueness._computed` for computed grouping items | `test_paulley_audit.py` G2 |
| G3 | Grouping attributes → ι; no grouping attributes: one row | Claim 22, p.83 | implemented | `uniqueness._functional_dependencies` (Aggregate: grouping outputs → ι; a scalar aggregate has none, so ∅ → ι) | `test_paulley_audit.py` G3, G3e |
| G4 | Aggregate is determined by its group: ι → f | Claim 23, p.84 | implemented | ι → every column (`_row_attributes`); our extension: MIN/MAX/ANY_VALUE of a determined column pass it through (`AnyAggregate`) | `test_paulley_audit.py` G4 |
| L1 | Left join keeps the preserved side's strict FDs | Thm 3 strict 1, p.96 | pending | | |
| L2 | Null-supplying strict FD X → Y survives when η(p, X) | Lemma 7, Thm 3 strict 2, p.87 | pending | | |
| L3 | …or when X = Y is a strict equivalence in the null-supplying input | Corollary 2, p.88 | pending | | |
| L4 | Null-supplying lax FD becomes strict when η holds for every attribute in it | Lemma 8, p.89 | pending | | |
| L5 | ON equality within the null-supplying side: strict when η(p, X) | Lemma 9 case 2, Thm 3 strict 3, p.93 | pending | | |
| L6 | All preserved attributes of p determine a null-supplying attribute p binds, when η(p, Y) | Lemma 9 case 3, Thm 3 strict 4, p.93 | pending | | |
| L7 | Left join: ι(Q) ↔ ι(S), ι(T) | Thm 3 strict 5, p.96 | pending | | |
| L8 | Left join keeps both sides' lax FDs | Thm 3 lax 1–2, p.96 | pending | | |
| L9 | Null-supplying strict FD without η degrades to lax | Thm 3 lax 3, p.96 | pending | | |
| L10 | Other ON-derived FDs touching the null-supplying side are lax | Lemma 9 case 4, Thm 3 lax 4, p.93 | pending | | |
| L11 | ON conditions only on the preserved side give nothing | Lemma 9 case 1, p.93 | pending | | |
| L12 | Lax equivalence in T strengthened when η holds for both | Thm 3 strict eq 3, p.97 | pending | | |
| L13 | ON equality within T: strict equivalence | Thm 3 strict eq 4, p.97 | pending | | |
| L14 | Cross-side ON equality: lax equivalence only | Thm 3 lax eq 3, p.97 | pending | | |
| L15 | Null constraints among the null-supplying side's definite attributes; a later null-intolerant predicate makes them all definite | Def 38, Thm 3 null, p.95 | pending | | |
| F1 | Full join: input strict FD survives when η(p, X) or a strict equivalence | Thm 4 strict 1, p.100 | pending | | |
| F2 | Full join: cross-side ON FD αR(p) → Y with η(p, Y) and η(p, αS(p)) | Lemma 10 case 3, Thm 4 strict 2, p.100 | pending | | |
| F3 | Full join: ι(Q) ↔ ι(R), ι(S) | Thm 4 strict 3, p.100 | pending | | |
| F4 | Full join keeps both sides' lax FDs; cross-side ON FDs lax | Thm 4 lax, p.101 | pending | | |
| F5 | Full join: ON conditions within one side give nothing | Lemma 10 cases 1–2, p.99 | pending | | |
| F6 | Full join: equivalences of inputs kept; cross-side ON equality lax | Thm 4 eq, p.101 | pending | | |
| F7 | Full join: a key of the result needs η for each input key | §3.2.10.1, p.99 | pending | | |
| F8 | Full join: null constraints within each side | Thm 4 null, p.101 | pending | | |

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
- [x] **Deterministic expressions** — `inputs → f(inputs)` (Paulley §3.1.4).
  Built-in functions and operators only: a user-defined (unknown) function,
  `random`, `uuid`, `current_*`, an aggregate, window or subquery is not a
  function of its columns. Every input must compare by identity
  (`OutputColumn.identity_comparison`): under `NOCASE` `'a' = 'A'` and a
  DOUBLE's `-0.0 = 0.0`, yet a function can tell them apart.
- [ ] **Injective expressions** — a curated allowlist (lossless casts,
  `x ± constant`); everything else non-injective.
- [x] **Strict/lax FDs in the left-join rules** above, including η.
- [x] **Set-operation rules** — UNION / INTERSECT / EXCEPT without `ALL` key
  all their columns; INTERSECT and EXCEPT keep the left input's dependencies.
  Not yet: tagged `UNION ALL`. The right arm's columns are paired by
  position (`SetOperation.arms`) for nullability and domains, not yet for FDs.
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
  - Alias, Named Relation, Filter, Project, LIMIT, QUALIFY: one output row is one
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
- A known hard LIMIT of zero or one: `∅ → RowIdentity`, regardless of input keys.
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
  conjuncts `a = b` between columns of known matching comparison types,
  without collation. Implicit casts and unknown types never merge groups.
  An outer join's ON never merges, not
  even within its padded side; neither does `a = f(b)`, a disjunction, or a
  semi or anti join's ON.
- Input groups pass through every column that passes an input column
  through: Alias, Named Relation, Filter, Project, LIMIT, both sides of a join,
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

### Referential Coverage through queries

`sqlassert/rules/foreign_key.py`. `referential_coverage(X, Y)` walks two
directions, never mixing them:

- **Referenced side, upward.** Coverage onto an input carries over to a
  relation that keeps every input row's values: Alias, Named Relation body,
  Project, DISTINCT, grouping keys, the preserved side of an outer join, either
  branch of a UNION. `_targets(Y)` lists Y and every such upstream column list,
  paired by position. A narrowing relation (Filter, LIMIT/OFFSET, QUALIFY, inner join,
  INTERSECT, EXCEPT) stops it: the rows the referencing values match may be
  the dropped ones. A proved Exactly One inner join keeps every left row, so
  its left columns carry coverage over.
- **Referencing side, downward.** X holds on a relation when each output row
  takes X from one row of one covered input: Alias, Named Relation body,
  Filter, Project, LIMIT, DISTINCT, QUALIFY, grouping keys, and either side of any
  join -- a padded side is NULL in every column, which `MATCH SIMPLE` exempts.
  A UNION needs both branches covered, INTERSECT either, EXCEPT the left.
- **Equality.** Each column of X may stand in for any column of its
  [equality group](#equality-groups): where it is non-null, they are equal. So
  `e JOIN u ON e.user_id = u.id` covers `e.user_id` onto `u.id`, then onto
  `users.id` -- including onto a narrowed `u`, which coverage of `users` alone
  never reaches.
- At each step, coverage holds when known coverage pairs X with a target in
  any order, or X is a target. Anything else -- a computed or aggregated
  column, columns from both sides of a join -- stops propagation, and the
  explanation names where; at a base table it names the missing Foreign Key
  and any narrowed target.

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
- **Optimization hints** from proved dependencies (Lindner et al.,
  [`references.md`](references.md)) are `suggest`'s Improvements
  (`sqlassert/improvements.py`, issue 15): a redundant `GROUP BY` column
  another grouping column determines, a redundant `DISTINCT`, a join whose
  right side is unique and contributes no used column (removable, or "join
  only filters"), and a LEFT JOIN that padding promotion makes inner.

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
