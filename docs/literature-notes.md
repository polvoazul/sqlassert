# Literature Notes

What sqlassert can take from the papers in `papers/`. Companion to
[`functional-dependencies.md`](functional-dependencies.md). Page and section
numbers refer to each paper's printed numbering.

## How our goal differs

All four papers are about **query optimizers**: they use dependencies to
rewrite plans into equivalent, cheaper ones. sqlassert is an **author-facing
proof tool**: it proves a property holds for a query and explains why.

| | Optimizers (papers) | sqlassert |
|---|---|---|
| Fact must hold on | the current data (Kossmann §1, §3.5; Lindner re-validates per ETL run) | every database the schema allows |
| Dependency sources | profiling the data, app code (Liu), declared constraints | DDL, catalog, author `DECLARE`s, query structure |
| Obligation | rewritten plan ≡ original (Liu needs a U-semiring verifier) | one property of one query — weaker, checkable by local rules |
| Missing a dependency | lost optimization | `UNKNOWN` — both accept incompleteness |
| Output | a faster plan | an evidence tree a human reads |

Consequences:

- Data-discovered dependencies (HyUCC, HyFD, …) can at most *suggest* a
  `DECLARE`; never back a `PROVED`. Liu Table 9 shows data-mined keys are
  often spurious (`users.creation_time`, `encrypted_password`).
- Every optimizer rewrite has a precondition that is exactly a property we
  prove (unique join, strict key, non-null). Their rule catalogs are a
  roadmap of assertions worth supporting.
- Each derived FD/key must carry its source, so `explain()` stays readable.
  Optimizers do not need this.

## Corrections to our FD plan (Paulley 2000)

- **Three kinds of key, not two.** Besides lax (Unique Set) and non-null strict
  (Non-Null Unique Set) there is a *null-equal strict* key: NULL compares
  equal to NULL. `DISTINCT`, `GROUP BY`, and set operators produce it even
  over nullable columns (Claims 18, 22). GROUP BY elimination needs only this,
  not a Non-Null Unique Set. Do **not** treat DuckDB `UNIQUE` as one: it is
  ANSI (multiple NULLs allowed), so it is lax (Claim 16, p.73).
- **Union-find merges strict equivalences only.** Lax transitivity is unsound
  unless the middle column is non-null (fd4b, fd7b, eq9b; counterexample
  Fig. 2.1 p.38). Lax ON-clause equalities need separate edges.
- **Lax becomes strict** once all involved columns are known non-null (fd6,
  eq7). Null-rejecting predicates spread non-nullness across strict
  equivalences.
- **Left join (Thm 3, p.96).** A right-side FD `X → Y` stays strict when some
  `x ∈ X` is non-null **or rejected by the ON condition when NULL** (η, Def 37).
  The second case covers the common `l.x = r.y` right column; our doc only had
  the first. ON-clause FDs follow Lemma 9's four cases: *all* left columns
  referenced in ON determine a right column bound by it, including right-side
  constants (Example 20: `{T.X, T.Y, T.Z} → S.D` from `S.D = 2`).
- **Null constraint (Def 38).** The non-null columns from the right side are
  all NULL together. A later null-rejecting `WHERE` on one makes them all
  non-null, which turns lax FDs strict.
- **MIN/MAX pass-through is our extension**, not the thesis's (Claims 22–23
  treat every aggregate as a fresh value). Sound only when `g → x` is strict.
- **Set operators** (§3.2.5–3.2.7), missing from our doc:
  - INTERSECT (ALL): FDs and keys of both inputs hold; corresponding columns
    are strictly equivalent.
  - EXCEPT: everything of the left input holds.
  - UNION ALL: no FDs in general; only equivalences and constants common to
    both branches. UNION (distinct): all output columns form a key.
  - Ours, not the thesis's: branches tagged with distinct literals make
    `(tag, branch key)` a key.

## Ideas to adopt

- **Keys as FDs to a row identifier** (Paulley Def 27, §3.1.2). Give each
  relation a virtual column ι; a key K is the FD `K → ι`. Uniqueness becomes
  "does `closure(S)` reach ι?", one test instead of key sets plus FDs. Joins:
  `ι(L) ∪ ι(R) → ι(out)` (Claim 19); a Unique Join is `ι(L) → ι(out)`; an input
  with at most one row lets the other side's ι determine everything (p.254).
- **FD-graph** (Paulley, from Ausiello–D'Atri–Saccà): a hypergraph with
  compound vertices for multi-column left sides. Closure is BFS with counters,
  O(V²) (§3.6.1, Lemma 29). Constants seed it. The lax variant steps only
  through non-null columns.
- **Projection marks hidden columns** instead of deleting them, so transitive
  FDs survive without recomputing closure (§3.2.2). Fits our IR, where every
  stage already keeps a reference to its input.
- **Predicates worth recognizing in `Filter`** (pp.77–78): `col = col`,
  `col = const`, `IS NOT NULL`, `IS NULL` (equal to a NULL constant), `LIKE`
  without wildcards (equality), `= (uncorrelated scalar subquery)` (unknown
  constant). Disjunctions ignored. Correlated outer columns are constants
  inside EXISTS.
- **Rewrite preconditions as assertion ideas** (Kossmann Table 1; Lindner
  §3.2): redundant DISTINCT, redundant GROUP BY columns, EXISTS → join, join
  elimination / join → semi-join (UCC on the join key, no right columns used),
  scalar subquery single-valued (WHERE pins an FD's determinant),
  INTERSECT/EXCEPT → ALL.
- **"Exactly one" joins = IND + key** (Lindner O-3; Kossmann §8.1). The
  foreign-key column must be non-null, or the IND null-equal. An IND
  `R.a ⊆ S.x` dies under any selection on S except `x IS NOT NULL`, and
  survives `S ⋈ T` when `S.y ⊆ T.u` (Lindner Table 1). Paulley has nothing on
  INDs.
- **ORM code as a knowledge source** (Liu Tables 1–2): Rails
  `validates_uniqueness_of :f, scope: [...]` → composite Unique Set;
  `validates_presence_of` → non-null; `belongs_to` → FK; `has_one` → Unique
  Set on the FK; `validates_inclusion_of` / STI `type` / `state_machine` →
  ENUM domain (TODO Task 4). Only 12% were declared in the DB. Rails
  validations are not race-safe, so these are `DECLARE`-level trust at best.
- **Audit scripts for declarations** (Liu §5.3; Lindner §5): generate SQL that
  checks each `DECLARE` against real data (`count(DISTINCT k) = count(k)`, an
  anti-join for an FK, a NULL count). Optional audits; never input to a proof.

## Testing and guarantees

- **Differential soundness test with tiny instances** — the strongest idea.
  Paulley's Theorem 11 (p.196) is an iff condition for uniqueness in
  select-project-join queries, and its proof builds every counterexample from
  **two rows per table**; Theorem 12 (p.207) does the same for "at most one
  match". So for each `PROVED` assertion: generate small random instances that
  satisfy the DDL (Hypothesis), run the query on DuckDB, and assert the
  property holds. For SPJ queries, ≤2 rows per table is complete; for the rest
  it is a strong random check.
- **Counterexamples → `UNKNOWN` tests** (Paulley): Fig. 2.1 p.38 (lax
  transitivity), Example 16 p.85 (strict FD broken by padded row), Example 18
  p.91 (null-tolerant ON), Example 19 p.92 (extra ON conjunct), Example 24
  p.193 (DISTINCT needed).
- **`PROVED` tests** (Paulley): Examples 9, 17, 20, 23, 25–31.
- **Small SQL cases** (Kossmann §5.1, 5.3–5.5, 6.1, 6.3, 7.2, 8.1–8.2): e.g.
  TPC-H Q3 `GROUP BY l_orderkey, o_orderdate, o_shippriority` → `l_orderkey`.
- **Redmine query** (Liu Listings 2/10): `SELECT DISTINCT users.* … JOIN
  members ON members.user_id = users.id WHERE members.project_id = $2` with
  `(user_id, project_id)` unique → DISTINCT redundant. Exercises the
  constant-bound key part.
- **Benchmarks with known dependencies** (Lindner Table 2, §7.4): TPC-H,
  TPC-DS, SSB, JOB. Facts like `lineitem.l_orderkey ⊆ orders.o_orderkey`;
  TPC-H Q10 group-by shrinks from seven columns to one. TPC-H/TPC-DS DDL with
  their PKs/FKs makes a realistic end-to-end suite.
- **DuckDB does not exploit PKs/FKs** (Lindner Fig. 5, DuckDB 1.1.3): a proof
  from sqlassert is what would justify a manual rewrite there.

## Links

Code and artifacts:

- Lindner et al. (Hyrise dependency-based optimization):
  https://github.com/HPI-Information-Systems/dependency-based-qo
- Liu et al. (ConstrOpt, Rails constraint extraction):
  https://github.com/LiuXiaoxuanPKU/ConstrOpt
- PostgreSQL patch for UCC propagation / DISTINCT removal:
  https://commitfest.postgresql.org/35/2433/
- Metanome (dependency profiling): https://bit.ly/metanome
- z3 (Python): https://pypi.org/project/z3-solver/
- jOOQ on join elimination:
  https://blog.jooq.org/2017/09/01/join-elimination-an-essential-optimiser-feature-for-advanced-sql-usage/

Benchmarks:

- TPC-H: http://tpc.org/tpc_documents_current_versions/pdf/tpc-h_v3.0.1.pdf
- TPC-DS: http://tpc.org/tpc_documents_current_versions/pdf/tpc-ds_v3.2.0.pdf
- SSB: https://www.cs.umb.edu/~poneil/StarSchemaB.PDF

Systems named without code: Sybase / SAP SQL Anywhere (shipped Paulley's
simplified FD-graph), Hyrise, DB2/Starburst, Oracle 8i dimensions (declared
FDs), SAP HANA, Umbra, MonetDB. Cosette/UDP (Chu et al. 2018) is the
equivalence verifier Liu uses.

Possible text-extraction errors in Paulley, to check against the PDF:
Thm 3 lax-equivalence bullet 3 (`∩ α(T) = ∅`, likely `≠ ∅`), and the
definiteness condition in Dependency-closure.
