# Clingo Rules Rebuild Assessment

Date: 2026-08-29

## Scope

This assessment reasons forward from three sources only:

- the encoded `ir__*` semantic object graph;
- the encoded `pub__*` Knowledge facts;
- the behavioral objectives expressed by the test suite.

It deliberately does not preserve or translate the retired rule vocabulary. The proposed rebuild target is the logic program and its reporting predicate names, not the parser, lowering, or IR.

At the time of investigation, the full suite had 240 tests: 154 passed and 86 property-engine tests failed. This is a useful migration state. Parsing, lowering, IR construction, encoding tests, diagnostics, and other non-property behavior mostly remain operational while semantic derivation is disconnected.

## Main property-engine objectives

The engine needs to answer a small collection of semantic questions.

### 1. What Unique Sets are known for a Relation Expression?

A Unique Set needs:

- its own solver identity;
- the Relation Expression to which it belongs;
- its ordered Output Column members.

The public representation is:

```prolog
pub__unique_set(Key, Relation).
pub__unique_set_column(Key, Position, Column).
```

### 2. Which Output Columns are known non-null?

```prolog
pub__non_null_column(Relation, Column).
```

Non-nullness is tied to a particular Relation Expression occurrence and its Output Column identity. It must not leak between aliases or unrelated relations that happen to use the same column name.

### 3. Is an arbitrary column set unique?

An arbitrary set of columns on a Relation Expression is unique when it contains every member of at least one known Unique Set on that relation. It may contain additional columns.

This accounts for behavior such as proving `UNIQUE(id, name)` from an already known Unique Set `(id)`.

### 4. Is an arbitrary column set non-null unique?

It must satisfy both conditions:

- it contains a known Unique Set;
- every column in the arbitrary set is independently known non-null.

This is the common semantic question behind Candidate Key assertions. In the current domain language, minimality is not separately proved; a Non-Null Unique Set is treated as a Candidate Key.

### 5. Which Relation Expressions establish Unique Sets structurally?

- An Aggregate establishes a Unique Set from its complete Grouping Key.
- A Distinct establishes a Unique Set from every output column participating in duplicate removal.
- A QualifyByPartition establishes a Unique Set from its complete Partition Key.

Unsupported shapes are lowered to opaque relations or otherwise withheld from these concrete IR forms, so logic should reason only from the supported IR types it receives.

### 6. Which operations transport properties?

Properties can move through fresh Output Columns when the target column directly passes through a source Output Column.

The principal property-preserving operations are:

- Alias;
- a Named Relation with a body;
- Filter;
- Project;
- Distinct;
- QualifyByPartition;
- the left input of a Join, conditionally, once the Join is proved unique.

Aggregate should initially establish its Grouping Key directly rather than participate in broad generic source-property propagation. That is sufficient for the current behavioral objectives and avoids asserting wider aggregate preservation semantics prematurely.

### 7. Does a Join admit at most one right-side match per left row?

For the supported INNER and LEFT join kinds, the answer is yes when the Join condition constrains every member of some Unique Set on the right input.

A right-side column is constrained by an understood equality to something independent of the right input:

- a scalar Constant; or
- a direct ColumnRef outside the right input, normally from the left input.

Range predicates, null-safe equality, `OR`, opaque expressions, unsupported join kinds, and ambiguous `USING` lowering do not establish coverage.

### 8. What durable proof and diagnostic evidence must consumers receive?

The reporter needs public facts for:

```prolog
pub__proved(Assertion).
pub__proof_key(Assertion, Key).
pub__proved_by_candidate_key(Assertion).
pub__assertion_missing_member(Assertion, Key, Column).
```

Proof evidence identifies the actual Unique Set witness. Missing-member evidence explains an UNKNOWN outcome without turning lack of proof into a Disproof.

## Immediate public-encoding prerequisite

The current reflected Knowledge encoding emits facts shaped like:

```prolog
pub__unique_set(Relation).
pub__unique_set_column(Key, Position, Column).
```

The Unique Set fact has lost the identity referenced by its member facts. For example, two Unique Sets on one relation collapse into repeated unary relation facts, while their columns point to different generated Knowledge symbols that cannot be joined back to the relation.

The contract must first become:

```prolog
pub__unique_set(Key, Relation).
pub__unique_set_column(Key, Position, Column).
```

This is an encoding responsibility because the Key is a solver transport identity and must not enter the framework-independent Knowledge object graph. The first TDD step should check exact predicate names and arities, rather than checking only that a fact string contains `pub__unique_set(`.

## Central reasoning kernel: arbitrary column sets

The highest-leverage intermediate representation is a generic column-set question:

```prolog
column_set(Context, Relation).
column_set_member(Context, Column).
```

`Context` identifies why the set is being considered. An assertion and a Join's constrained right-side columns become two adapters into the same kernel.

### Unique Set containment

```prolog
candidate_unique_set(Context, Key) :-
    column_set(Context, Relation),
    pub__unique_set(Key, Relation).

missing_unique_set_member(Context, Key, Column) :-
    candidate_unique_set(Context, Key),
    pub__unique_set_column(Key, _, Column),
    not column_set_member(Context, Column).

covers_unique_set(Context, Key) :-
    candidate_unique_set(Context, Key),
    not missing_unique_set_member(Context, Key, _).
```

This handles:

- single-column and composite Unique Sets;
- arbitrary supersets;
- missing-member evidence;
- identical containment semantics for assertions and joins.

### Non-null column sets

```prolog
nullable_column_set_member(Context, Column) :-
    column_set(Context, Relation),
    column_set_member(Context, Column),
    not pub__non_null_column(Relation, Column).

non_null_column_set(Context) :-
    column_set(Context, _),
    not nullable_column_set_member(Context, _).

non_null_unique_column_set(Context, Key) :-
    covers_unique_set(Context, Key),
    non_null_column_set(Context).
```

This directly answers the main primitive question: whether an arbitrary set of Output Columns is non-null unique.

## Semantic predicates over the IR

Rules should read concrete `ir__*` facts directly whenever possible. Internal unprefixed predicates should add semantic meaning rather than merely rename every encoded field.

### Property inputs

```prolog
property_input(Target, Source).
```

This unifies the concrete IR operations through which relevant relational properties can move. It is derived from fields such as:

- `ir__alias__source/2`;
- `ir__named_relation__body/2`;
- `ir__filter__input/2`;
- `ir__project__input/2`;
- `ir__distinct__input/2`;
- `ir__qualify_by_partition__input/2`.

For Join, `property_input(Join, Left)` is derived only after the Join is proved unique.

### Pass-Through Columns

```prolog
passes_column(Target, SourceColumn, TargetColumn).
```

It means:

- SourceColumn is an output of the selected property input;
- TargetColumn is an output of Target;
- TargetColumn's expression is a ColumnRef directly referencing SourceColumn.

Conceptually:

```prolog
passes_column(Target, SourceColumn, TargetColumn) :-
    property_input(Target, Source),
    ir__relation_expr__output_columns(Source, _, SourceColumn),
    ir__relation_expr__output_columns(Target, _, TargetColumn),
    ir__output_column__expression(TargetColumn, Expression),
    ir__column_ref(Expression),
    ir__column_ref__column(Expression, SourceColumn).
```

This is the shared basis for both Unique Set and non-null propagation.

### Non-null propagation

```prolog
pub__non_null_column(Target, TargetColumn) :-
    property_input(Target, Source),
    pub__non_null_column(Source, SourceColumn),
    passes_column(Target, SourceColumn, TargetColumn).
```

### Unique Set propagation

A source Unique Set propagates only when every source member has a usable Pass-Through Column on the target. A conservative MVP can reject ambiguous mappings where the same source member is emitted more than once, rather than constructing every possible mapped-key combination.

Useful intermediate predicates are:

```prolog
propagation_candidate(SourceKey, Source, Target).
unmapped_unique_set_member(SourceKey, Target, SourceColumn).
ambiguous_unique_set_member(SourceKey, Target, SourceColumn).
propagated_unique_set(DerivedKey, SourceKey, Target).
```

The derived key receives a deterministic Clingo term such as `propagated_key(SourceKey, Target)`. Its member positions remain those of the source key, while its columns are the mapped target columns.

The ambiguous-mapping guard is conservative: `SELECT id AS a, id AS b` may initially remain UNKNOWN for inferred keys instead of generating an unsound combined set or requiring combinatorial key construction.

## Structural Unique Set rules

Derived identities should be deterministic Clingo function terms.

### Aggregate

```prolog
pub__unique_set(aggregate_key(Relation), Relation) :-
    ir__aggregate(Relation).

pub__unique_set_column(aggregate_key(Relation), Position, Column) :-
    ir__aggregate__grouping_outputs(Relation, Position, Column).
```

Only Grouping Key outputs participate. Aggregate expressions such as `COUNT(*)` do not.

### Distinct

```prolog
pub__unique_set(distinct_key(Relation), Relation) :-
    ir__distinct(Relation).

pub__unique_set_column(distinct_key(Relation), Position, Column) :-
    ir__distinct(Relation),
    ir__relation_expr__output_columns(Relation, Position, Column).
```

The lowering already represents unsupported `DISTINCT *` and `DISTINCT ON` shapes conservatively, so these rules should not attempt to reinterpret opaque relations.

### QualifyByPartition

```prolog
pub__unique_set(partition_key(Relation), Relation) :-
    ir__qualify_by_partition(Relation).

pub__unique_set_column(partition_key(Relation), Position, Column) :-
    ir__qualify_by_partition__partition_outputs(Relation, Position, Column).
```

Only the already-recognized `ROW_NUMBER() OVER (PARTITION BY ...) = 1` IR form reaches these rules.

## Unique Set Assertion adapter

An assertion's named columns become a column-set Context:

```prolog
column_set(assertion_set(Assertion), Relation) :-
    ir__unique_set_assertion__subject(Assertion, Relation).

column_set_member(assertion_set(Assertion), Column) :-
    ir__unique_set_assertion__columns(Assertion, _, Column).
```

### Ordinary Unique Set Assertions

`UNIQUE(...)` requires a known Unique Set covered by the assertion's columns.

### Candidate Key Assertions

`PRIMARY KEY(...)` requires:

- a known Unique Set covered by the assertion's columns;
- every asserted column to be independently known non-null.

The boolean IR field is represented positively only when true:

```prolog
ir__unique_set_assertion__is_candidate_key(Assertion).
```

False is represented by absence.

### Publishing a proved assertion

A proved assertion produces:

```prolog
pub__proved(Assertion).
pub__proof_key(Assertion, ProvingKey).
pub__unique_set(asserted_key(Assertion), Relation).
pub__unique_set_column(asserted_key(Assertion), Position, Column).
```

This enables an assertion on a view, CTE, or subquery body to become property knowledge for downstream relation occurrences.

### Preventing circular proof

A directly generated `asserted_key(Assertion)` must not be eligible to prove an assertion on that same Relation Expression. Otherwise one assertion could prove itself, or multiple assertions on the same expression could mutually prove one another.

Direct assertion-generated keys should therefore be tagged internally and excluded from assertion proof candidates. When a proved asserted key later propagates through a Named Relation, Alias, or other Relation Expression, the new propagated key identity becomes valid downstream evidence.

This preserves the intended distinction:

- assertions at one attachment site are parallel requirements;
- a proved assertion becomes Knowledge for later relational occurrences.

## Unique Join adapter

### Supported join kinds

The IR currently encodes `Join.kind` as a Python string, so facts contain quoted terms such as `"inner"` and `"left"`, not enum atoms.

Only these two kinds participate in Unique Join proof.

### Right-side constrained columns

The principal intermediate predicate is:

```prolog
right_column_constrained(Join, Column).
```

For each Equality attached through `ir__join__equalities/3`:

1. Treat either side symmetrically.
2. Recognize one side as a direct ColumnRef to an Output Column of the Join's right input.
3. Require the other side to be understood and independent of the right input.

Understood independent expressions for the MVP are:

- a Constant;
- a direct ColumnRef to a column that is not an output of the right input.

Opaque expressions do not constrain a key member. A right-side column equated to another right-side column also does not constrain it independently.

### Join column-set Context

```prolog
column_set(join_rhs(Join), Right) :-
    ir__join__right(Join, Right).

column_set_member(join_rhs(Join), Column) :-
    right_column_constrained(Join, Column).
```

The generic `covers_unique_set/2` kernel can now prove composite, nullable, constant-completed, `ON`, and lowered `USING` cases without separate key-coverage logic.

### Join uniqueness and property composition

A Join is unique when:

- its kind is supported;
- `join_rhs(Join)` covers at least one Unique Set.

The proving key becomes assertion evidence for a Unique Join Assertion whose subject is that Join.

Once the Join is known unique, derive:

```prolog
property_input(Join, Left).
```

The generic property propagation rules then carry the left input's Unique Sets and non-null columns into the Join's fresh output columns. This makes uniqueness compose through a proved-unique Join and then through any outer Project, Filter, Alias, CTE, view, or Root Select operation.

## Candidate Key classification

A known Unique Set is a Non-Null Unique Set when every one of its members is known non-null on its relation.

Useful internal predicates are:

```prolog
nullable_unique_set_member(Key, Column).
candidate_key(Key, Relation).
```

For a Unique Join Assertion, `pub__proved_by_candidate_key(Assertion)` means its proving right-side Unique Set is a Candidate Key.

For a `PRIMARY KEY(...)` assertion, it means the assertion was proved under the full non-null column-set requirement. The proving Unique Set may be smaller than the asserted superset; all asserted columns must still be non-null.

## Evidence and UNKNOWN diagnostics

Evidence should be derived after semantic proof behavior works.

For an unproved assertion:

1. Count the missing members of each candidate Unique Set.
2. Find the minimum missing-member count.
3. Retain all tied closest Unique Sets deterministically.
4. Publish their missing members as `pub__assertion_missing_member/3`.

Deterministic `#count` and `#min` aggregates are permitted by the rule policy. Evidence predicates must never feed back into property derivation.

The reporter should consume the `pub__*` namespace exclusively. Internal predicates remain implementation details of the logic program.

## Rule organization opportunity

Clingo rule files do not provide predicate namespaces; every unprefixed predicate is global even when definitions are split across files. A fresh reconstruction should therefore organize by semantic responsibility and avoid reusing generic names independently in multiple files.

A reasonable organization is:

1. `column_sets.lp` — generic coverage and non-null-set reasoning.
2. `property_transport.lp` — property inputs, Pass-Through Columns, and propagation.
3. `structural_unique_sets.lp` — Aggregate, Distinct, and QualifyByPartition facts.
4. `unique_set_assertions.lp` — assertion adapter, proof, and publication.
5. `unique_joins.lp` — equality understanding, right-side constraints, and Join proof.
6. `evidence.lp` — proof reporting and closest-missing-member diagnostics.

The precise filenames matter less than keeping one definition of each shared semantic predicate.

## TDD reconstruction sequence

### Step 1: repair the public fact contract

Add exact encoding tests for:

- `pub__unique_set/2`;
- `pub__unique_set_column/3` referencing the same Key;
- `pub__non_null_column/2`;
- multiple Unique Sets on the same relation remaining distinct.

Verification: IR encoding tests pass and a supplied Unique Set can be joined to its member facts.

### Step 2: add a small rule-test harness

Solve small synthetic `ir__*` and `pub__*` programs directly, independently of SQL parsing and lowering.

Initial rule-level cases:

- exact single-column coverage;
- complete composite coverage;
- one missing composite member;
- a superset covering a smaller Unique Set;
- nullable unique versus non-null unique column sets;
- two relation occurrences with identical names or shapes remaining distinct.

Verification: the generic column-set kernel works without requiring an end-to-end SQL fixture.

### Step 3: implement base public-property reporting

Update reporting to consume `pub__*` facts and prove that declared table Knowledge appears in `RelationFacts`.

Verification: the basic declared primary/composite-key relation-fact tests pass.

### Step 4: implement property transport

Proceed through increasingly long paths:

1. table to Alias;
2. Filter;
3. Project and renamed columns;
4. Named Relation bodies;
5. CTE and view chains;
6. Distinct and Qualify preservation where applicable.

Verify Unique Sets and non-nullness together, since Candidate Key behavior depends on both following the same Output Column identities.

### Step 5: implement structural Unique Sets

Implement and test separately:

- Aggregate Grouping Keys;
- Distinct output sets;
- QualifyByPartition Partition Keys.

Verify complete composite keys, aliases, unsupported shapes, and nested structural operations.

### Step 6: implement Unique Set Assertions

Order the behavior:

1. ordinary single-column proof;
2. composite and superset proof;
3. Candidate Key non-null requirement;
4. multiple parallel assertions;
5. assertion publication;
6. downstream feed-forward through a new relation occurrence;
7. explicit tests preventing self-proof and mutual proof.

### Step 7: implement Unique Joins

Order the behavior:

1. a single right-side Unique Set column constrained by left equality;
2. composite coverage;
3. a key member constrained by a Constant;
4. incomplete coverage;
5. nullable Unique Set versus Candidate Key evidence;
6. INNER and LEFT kinds;
7. unsupported join kinds;
8. opaque, range, null-safe, and ambiguous predicates.

### Step 8: enable Join property propagation

After Join proof is stable, make a unique Join a conditional property input from its left side.

Verification: left-side keys survive the Join and can prove assertions after outer projection, filtering, grouping tails, CTE/view wrapping, and Root Select lowering.

### Step 9: add evidence

Add:

- `pub__proof_key/2`;
- Candidate Key proof flags;
- closest known Unique Set selection;
- ordered missing-column reporting.

Verification: explanation and `missing_columns` tests pass without changing proof outcomes.

### Step 10: run the whole acceptance suite

Run:

- rule-level tests;
- focused end-to-end property groups;
- stable-model policy tests;
- forbidden-rule-construct tests;
- the full suite.

The final criterion is one deterministic stable model and all existing behavioral tests passing.

## Recommendation

Replace the current `.lp` implementation rather than translating it predicate by predicate. Preserve the behavioral tests, the semantic IR, and the overall `ir__`/`pub__` decision.

The reconstructed logic should have:

- one public property store in `pub__*`;
- one generic arbitrary-column-set reasoning kernel;
- one property-transport implementation;
- small adapters for assertions and Joins;
- structural property sources for Aggregate, Distinct, and QualifyByPartition;
- evidence derived strictly after proof semantics.

The first implementation action should be the `pub__unique_set/2` encoding correction, because every subsequent rule depends on joining a Unique Set identity to both its relation and its members.
