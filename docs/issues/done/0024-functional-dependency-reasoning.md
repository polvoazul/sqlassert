# 24. Functional-dependency reasoning

- **State:** done
- **Labels:** epic, planned
- **Created:** 2026-11-01
- **Migrated from:** README Ideas, `TODO.md` Later Ideas

Represent and derive functional dependencies (`X → Y`): when equal values of
one column set guarantee equal values of another, including dependencies
created by keys, equality predicates, deterministic expressions, and
aggregation. Without them, uniqueness only flows through columns that pass
through unchanged.

Theory, propagation rules, and the representation design live in
[`docs/functional-dependencies.md`](../../functional-dependencies.md#representation-design-draft);
that doc is the source of truth for *what*. This issue is the plan for *how*.

## Plan

Replace, don't migrate (see [`AGENTS.md`](../../../AGENTS.md)): Functional
Dependencies and Row Identities replace the current unique-set propagation
outright. The user-facing contract stays: Property Markers and the Properties
they introduce keep their syntax and meaning, and every existing `analyze()`
test must end green. Work test-first.

1. **IR prerequisites** (from [issue 23](../0023-close-ir-lowering-gaps.md)):
   - [ ] `Filter` carries recognized conjuncts: `col = col`, `col = constant`,
     `IS NOT NULL`, `IS NULL`; everything else opaque.
   - [ ] Join ON conjuncts beyond top-level `=`, enough to evaluate η.
   - [ ] Outer joins know which right-side columns are NOT NULL in the right
     input.
2. **Red: tests that pin the target.**
   - [ ] Paulley counterexamples as `UNKNOWN` tests: Fig. 2.1 p.38, Examples
     16, 18, 19, 24 (`docs/literature-notes.md`, Testing and guarantees).
   - [ ] Paulley worked examples as `PROVED` tests: Examples 9, 17, 20, 23,
     25–31.
   - [ ] The design doc's own cases: `WHERE tenant_id = 5` shrinking
     `(tenant_id, id)`; `u.id` standing in for `o.user_id`; `GROUP BY user_id,
     user_name` keyed on `user_id`; GROUP BY elimination; TPC-H Q3; the
     Redmine `DISTINCT` query.
3. **Null sources** (design doc, Null sources), replacing the boolean
   non-null analysis:
   - [ ] `null_sources(column)` → set of `Organic` / `Padding(J)`;
     `non_null_column(s)` become "the set is empty".
   - [ ] Padding promotion: a null-rejecting conjunct on a column carrying
     `Padding(J)` removes it from every column carrying it.
   - [ ] Organic removal per column; the ON condition removes `Organic` from
     the right columns it rejects.
   - [ ] Explanations name each source ("nullable in `users`", "padded by the
     LEFT JOIN on line 3").
   - [ ] Red tests first: LEFT JOIN + `WHERE u.id = 5` proves right-side keys;
     nested outer joins; a column nullable *and* padded.
4. **Engine vocabulary** (`docs/functional-dependencies.md`, Vocabulary in the
   rules; Strata):
   - [ ] `RowIdentity`, `FunctionalDependency` (strict / lax).
   - [ ] `_functional_dependencies(relation)` for every operator, with
     evidence per FD: Filter, Project, inner / left / full join (with η),
     Aggregate, Distinct, set operations, opaque relations, knowledge and
     declarations.
   - [ ] `_closure(relation, columns, strict=...)`: counter-based, lax steps
     only through determinants with no NULL source, `members` carry evidence.
   - [ ] New strata in `Stratum`, matching the planned order.
5. **Rewrite the public analyses on closure**, deleting what they replace:
   - [ ] `unique_set`, `non_null_unique_set`: closure reaches `RowIdentity`.
   - [ ] `unique_join`: equality-bound right columns reach the right input's
     `RowIdentity`.
   - [ ] Delete `_unique_sets`, `_preserving_input`, `_pass_through_columns`,
     and the column-sets-of-interest pass (closure covers supersets directly).
   - [ ] `RelationFacts` in the report: derive each named relation's keys from
     the FDs into its `RowIdentity`. **Decide** whether to report minimal keys
     only.
6. **`DETERMINES` Property and markers.**
   - [ ] `FunctionalDependency` Property; `/**ASSERT DETERMINES(a, b -> c)**/`
     and `/**DECLARE DETERMINES(...)**/` in `sql_parse.MARKERS`, with
     diagnostics for malformed lists.
   - [ ] Public analysis `functional_dependency(determinant, dependent)`.
   - [ ] README section and `CONTEXT.md` (glossary entry already added).
7. **Green and clean.**
   - [ ] Whole suite green; outcomes only change UNKNOWN → PROVED.
   - [ ] Update `docs/implementation-conventions.md`,
     `sqlassert/rules/STYLEGUIDE.md`, and `CONTEXT.md` (e.g. *Derived Unique
     Set*) to the new mechanism; remove docs of deleted code.

Decided: padding tracking (`matched`) is in the first cut, as null sources.
Decided (step 5): `RelationFacts` reports minimal keys only. `suggest`
already reports minimal sets, a superset of a reported key is unique anyway
(`is_unique` answers for it), and with closure every superset is a key, so
listing them would be noise. Keys are enumerated smallest first up to three
columns, plus one shrunk key when none is that small. Two existing facts
expectations changed accordingly (a proved superset and a Distinct whose key
shrank by a dependency); no assertion outcome did.

Follow-ups, not in this plan: a null-equal strict key Property (needs a name
and a marker), window functions, deterministic and injective expressions,
equality groups for value facts ([issue 26](0026-equality-groups.md)),
assertions from optimizer preconditions.

## Someday/maybe (from `TODO.md` Later Ideas)

- [ ] Someday/maybe: optimization hints from proved dependencies. The facts that
  prove properties also justify rewrites (see
  [`docs/references.md`](../../references.md), Lindner et al.): report a
  redundant `GROUP BY` column when another grouping column determines it, and a
  join whose RHS is unique and contributes no output columns ("join only
  filters", a semi-join candidate). Needs functional dependencies first.
