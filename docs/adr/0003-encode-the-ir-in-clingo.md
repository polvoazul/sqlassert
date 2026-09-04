# Encode the IR in Clingo

## IR to logic encoding

The `ir__` namespace encodes the semantic IR inside the logic engine. The `pub__` namespace is the public logic API: producers assert it directly, rules use and produce it directly, and consumers read it directly. Unprefixed predicates are internal logical bridges.

In Clingo, `ir__filter(f1)` is an atom, `ir__filter(f1).` is a ground fact, and `ir__filter/1` is the predicate signature.

```text
ir__<class>
ir__<class>__<field>
pub__<predicate>
```

### Node types and inheritance

Node types are unary predicates:

```prolog
ir__filter(f1).         % f1 is a Filter
ir__column_ref(e1).     % e1 is a ColumnRef
ir__output_column(c1).  % c1 is an OutputColumn
```

Abstract IR classes use an `abstract=True` class keyword handled by `NodeMeta` and cannot be constructed:

```python
class Node(metaclass=NodeMeta, abstract=True):
    ...

class ScalarExpr(Node, abstract=True):
    ...

class RelationExpr(Node, abstract=True):
    ...

class Filter(RelationExpr):
    ...
```

Only the concrete type is emitted as a fact. Python inheritance becomes generated logic rules:

```prolog
ir__relation_expr(Node) :- ir__filter(Node).  % Filter extends RelationExpr
ir__relation_expr(Node) :- ir__project(Node).
ir__relation_expr(Node) :- ir__join(Node).

ir__scalar_expr(Node) :- ir__column_ref(Node).

ir__node(Node) :- ir__relation_expr(Node).
ir__node(Node) :- ir__scalar_expr(Node).
ir__node(Node) :- ir__output_column(Node).
```

### Fields

Scalar fields and object references put the object first:

```prolog
ir__filter__input(f1, source1).                       % Filter.input
ir__output_column__name(c1, "user_id").              % OutputColumn.name
ir__output_column__scalar_expr(c1, e1).               % OutputColumn.scalar_expr
ir__column_ref__column(e1, source_column1).            % ColumnRef.column
ir__named_relation__role(r1, view).                   % NamedRelation.role
```

Ordered collections include their zero-based position:

```prolog
ir__relation_expr__output_columns(r1, 0, c1).  % RelationExpr.output_columns[0]
ir__relation_expr__output_columns(r1, 1, c2).  % RelationExpr.output_columns[1]
```

Unordered collections emit membership facts without a position:

```prolog
pub__unique_set__columns(k1, c1).
pub__unique_set__columns(k1, c2).
```

The encoder sorts unordered members by their assigned solver symbols before emitting facts so the encoded text is deterministic. That serialization order has no semantic meaning.

Every field of every reachable `Node` is encoded by default. Optional fields produce no fact for `None`; enums become lowercase atoms such as `view` and `inner`; user-provided text becomes a string. `Node.origin` is explicitly excluded because provenance remains outside Clingo. `Assertion.property` registers column sets of interest instead of query predicates, as described below. Unsupported values fail encoding.

### Boolean fields

Boolean fields begin with `is_` in the Python IR:

```python
class RelationExpr(Node, abstract=True):
    is_schema_complete: bool = False
```

Encoding preserves those names. True produces a unary fact; false produces no fact:

```prolog
ir__relation_expr__is_schema_complete(r1).
```

```prolog
schema_incomplete(Relation) :-
    ir__relation_expr(Relation),
    not ir__relation_expr__is_schema_complete(Relation).
```

### Assertions query Properties

An Assertion wraps exactly one Property:

```python
class Assertion(Node):
    property: Property
```

The Property carries the relevant graph references: a `UniqueSet` has its
columns, while a `UniqueJoin` has its join. The wrapper requests proof; it does
not make the Property true. Assertion kinds and graph references remain in
Python for reporting; the encoder does not flatten them into assertion-query
predicates. Unique Set and Candidate Key assertions register the column set
under analysis:

```prolog
pub__column_set_of_interest(asserted_columns(a1)).
pub__column_set_of_interest__columns(asserted_columns(a1), c1).
```

Only assertions register sets of interest. These descriptive facts do not
establish uniqueness. When a set of interest contains a known unique set,
the rules publish its unique-set membership; the general membership rule then
derives `pub__unique_set(Set)`.
Unrequested supersets are not generated. An ordinary Unique Set assertion
is reported as proved by directly checking
`pub__unique_set(asserted_columns(Assertion))`. Candidate Key assertions check
`pub__candidate_key` for that same set identity, so uniqueness alone cannot
satisfy them. No assertion-result predicate feeds back into property inference.

Join coverage retains its internal column sets and does not register sets of
interest or use this superset generalization to create new unique sets.
The rules publish established Unique Join properties through
`pub__unique_join__join/2` and `pub__unique_join/1`, using the same public shape
as declarations. Reporting checks for a public Unique Join property referencing
the assertion's join. Accepted and derived Unique Join properties both preserve
left-side unique sets.

Reporting reads these public properties directly to determine assertion
outcomes. It uses coverage and missing-member facts for explanations, without
reimplementing their inference or treating evidence as a property. This evidence is
keyed by column-set context (`asserted_columns(a1)` or `join_right_columns(j1)`),
and reporting associates it with assertions. The logic rules do not reference
assertion nodes.

### Public and internal logic

Knowledge and solver results share the public API:

```prolog
pub__unique_set(Key).
pub__unique_set__columns(Key, Column).
pub__non_null_column(NonNullFact).
pub__non_null_column__column(NonNullFact, Column).
pub__unique_join(Property).
pub__unique_join__join(Property, Join).
pub__covers_unique_set(Context, Key).
pub__missing_unique_set_member(Context, Key, Column).
```

Public predicates need no export wrappers. A Unique Set has no relation field: its Output Columns identify their own Relation Expression. Rules derive that association internally only when a question needs it:

```prolog
unique_set_on_relation(Key, Relation) :-
    pub__unique_set__columns(Key, Column),
    ir__relation_expr__output_columns(Relation, _, Column).
```

Other internal predicates connect the IR encoding to reasoning:

```prolog
unique_set_preserving_relation_operation(Target, Source) :-
    ir__filter__input(Target, Source).

unique_pass_through_column(TargetColumn, SourceColumn) :-
    unique_set_preserving_relation_operation(Target, Source),
    column_direct_reference(Target, Source, TargetColumn, SourceColumn).
```

### Accepted Properties

`properties.py` defines the IR-linked Property hierarchy shared by assertion queries,
declarations, and externally supplied knowledge:

```python
class Property(metaclass=NodeMeta, abstract=True): ...

class NonNullColumn(Property):
    column: OutputColumn

class UniqueSet(Property):
    columns: frozenset[OutputColumn]

class CandidateKey(UniqueSet): ...

class UniqueJoin(Property):
    join: Join
```

`Program.declarations` is a tuple of Properties accepted without proof.
`conversion.knowledge` is likewise a tuple of accepted Properties, currently
supplied by schema lowering. Both collections emit `pub__<property class>`
facts. `Program.named_relations` separately holds tables, views, and CTEs.

A Unique Set's columns are set members, not an ordered child collection, so
`pub__unique_set__columns/2` has no position argument. SQL lowering constructs
linked Properties directly. A database gatherer resolves qualified names before
constructing the same objects; the engine never resolves relation or column
names. Database hydration is tracked in [#14](https://github.com/polvoazul/sqlassert/issues/14).

`facts.py` assigns solver identities and states IR structure, column sets of interest,
and accepted Properties. Decisions such as which relation operations propagate
Unique Sets or Non-Nullness belong in logic rules.

### Generation

Generate IR facts and inheritance rules at runtime from Python introspection:

```text
Filter object               -> ir__filter/1 fact
Filter.input                -> ir__filter__input/2 fact
RelationExpr.output_columns -> ir__relation_expr__output_columns/3 facts
Filter extends RelationExpr -> ir__relation_expr/1 inheritance rule
```

`ClingoEncoding` keeps generated `inheritance_rules` separate from ground `facts`; `Engine` concatenates both with the semantic rule files before grounding.

The encoding module owns one deliberately small escape hatch:

```python
EXCEPTIONS = {
    ir.Node: lambda node, symbol, symbols: (),  # Do not reflect Node.origin
    ir.Assertion: lambda node, symbol, symbols: _assertion_interest_facts(node, symbol, symbols),
}
```

An exception replaces the default encoding for that class. Do not build a general customization framework until another concrete exception requires it.
