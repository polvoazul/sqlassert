# 13. Refactoring

- **State:** closed
- **Labels:** —
- **Created:** 2026-08-26
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/13

Replanned as a strict refactor: preserve every current proof and report behavior; add no lineage, functional dependencies, or general window semantics.

I added the deferred `Window` + `Filter` idea to [README.md](/Users/fred/src/sqlassert/README.md:187). Your existing lineage TODO remains untouched.

## Target model

```text
Program
├── declarations: NamedRelation[]
├── root: RelationExpr
└── assertions: Assertion[]
```

```text
Node
├── RelationExpr
│   ├── NamedRelation
│   ├── Alias
│   ├── Join
│   ├── Filter
│   ├── Project
│   ├── Aggregate
│   ├── Distinct
│   ├── SetOperation
│   ├── QualifyByPartition
│   └── OpaqueRelation
│       └── RecursiveRelation
├── ScalarExpr
│   ├── ColumnRef
│   ├── Constant
│   ├── Equality
│   └── OpaqueExpression
└── OutputColumn
```

Every `RelationExpr` has output columns. Each output column contains the expression producing it; `ColumnRef` points directly to an upstream output column. This is only the local structure needed for existing propagation—not a lineage system.

## Implementation sequence

1. Lock behavioral parity

Add characterization tests for:

- Self-joins with two aliases sharing one named table.
- Two references to the same view or CTE.
- Forward view references and CTE shadowing.
- Assertions on joins and relation outputs.
- Projection/filter/aggregate/distinct/qualify propagation.
- Current recursive-view and self-referencing-CTE outcomes.

Verify: current 226 tests still pass.

2. Replace the IR model

- Add the metaclass that automatically makes every `Node` subclass a frozen, identity-equality dataclass.
- Remove generated IDs from nodes.
- Store `Origin` directly.
- Add `OutputColumn`.
- Make `NamedRelation` a `RelationExpr` with `TABLE`, `VIEW`, or `CTE` role and an optional body.
- Make root and subquery status contextual rather than relation kinds.
- Keep `QualifyByPartition` unchanged as the explicit special case.
- Change assertions to direct references:

```text
UniqueJoinAssertion.subject → Join
UniqueSetAssertion.subject → RelationExpr
UniqueSetAssertion.columns → OutputColumn[]
```

3. Introduce linker-style resolution

- First collect table/view declarations.
- Maintain lexical CTE symbol tables.
- Resolve each table reference to one memoized `NamedRelation`.
- Wrap every occurrence in a fresh `Alias`, including implicit aliases.
- Build fresh output columns at each relational stage.
- Resolve column syntax into direct `OutputColumn` pointers.
- Mark schemas incomplete when the catalog does not provide every column.
- Detect resolution cycles and produce `RecursiveRelation`, still treated as opaque.

The resulting structure is a DAG because aliases share named-relation nodes.

4. Move IDs to the Clingo boundary

Create an encoding result containing:

```text
ClingoEncoding
├── facts
├── node_to_symbol
└── symbol_to_node
```

- Walk declarations and root in deterministic order.
- Visit shared nodes once using object identity.
- Assign Clingo symbols only during this walk.
- Use the reverse map for reporting assertion outcomes and origins.
- Remove `NameGiver` and `OriginRegistry` from `IrParser`.

5. Retarget facts and rules

Properties become attached to relation-expression outputs rather than synthetic definitions and instances.

Preserve only current inference:

- Alias passes properties through its column mapping.
- Filter preserves them.
- Project preserves fully retained Unique Sets.
- Aggregate is unique on grouping outputs.
- Distinct is unique on all outputs.
- `QualifyByPartition` is unique on partition outputs.
- A proven Unique Join preserves left-side properties.
- Opaque and Recursive relations contribute no properties.

No general dependency predicates are introduced.

6. Preserve reporting

- Named relations provide report labels.
- Anonymous relation expressions remain omitted from named relation facts.
- Assertion IDs in reports come from the encoding map.
- Public `analyze()` and `Report` behavior remain unchanged.

7. Remove the legacy structure

Delete:

- `RelationDefinition`
- `RelationInstance`
- `definition_id`, `instance_id`, and node `id`
- `instances()`, `all_instances()`, and `output_instance()`
- The join-output/true-instance bridge in the Clingo rules
- Converter-owned identifier generation

Update the glossary and supersede/clarify ADR 0002 so the documented boundary says direct semantic references internally and generated identifiers only at encoding.

## Completion criteria

- All existing and new tests pass.
- `ty check sqlassert tests` passes.
- Deterministic encodings are tested.
- Structurally equal aliases/scans remain distinct.
- Shared named relations receive one encoded identity.
- Recursive definitions cannot cause infinite traversal.
- No SQLGlot or Clingo type enters `ir/model.py`.
- No new proof behavior is introduced.

---

**Comment by polvoazul on 2026-08-27:**

The direct-reference immutable IR, linker-style resolution, Clingo-boundary identifiers, retargeted rules/reporting, deterministic encoding tests, glossary/ADR updates, and legacy-structure removal are complete across commits 2835fd1 through 1f3d822. Current verification: 236 tests pass and ty check passes. Closing as complete.
