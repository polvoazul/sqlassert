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
ir__output_column__expression(c1, e1).                % OutputColumn.expression
ir__column_ref__column(e1, source_column1).            % ColumnRef.column
ir__named_relation__role(r1, view).                   % NamedRelation.role
```

Ordered collections include their zero-based position:

```prolog
ir__relation_expr__output_columns(r1, 0, c1).  % RelationExpr.output_columns[0]
ir__relation_expr__output_columns(r1, 1, c2).  % RelationExpr.output_columns[1]
```

Every field of every reachable `Node` is encoded by default. Optional fields produce no fact for `None`; enums become lowercase atoms such as `view` and `inner`; user-provided text becomes a string. `Node.origin` is explicitly excluded because provenance remains outside Clingo. Unsupported values fail encoding.

### Boolean fields

Boolean fields begin with `is_` in the Python IR:

```python
class RelationExpr(Node, abstract=True):
    is_schema_complete: bool = False

class UniqueSetAssertion(Assertion):
    is_candidate_key: bool
```

Encoding preserves those names. True produces a unary fact; false produces no fact:

```prolog
ir__relation_expr__is_schema_complete(r1).
ir__unique_set_assertion__is_candidate_key(a1).
```

```prolog
schema_incomplete(Relation) :-
    ir__relation_expr(Relation),
    not ir__relation_expr__is_schema_complete(Relation).
```

### Public and internal logic

Knowledge and solver results share the public API:

```prolog
pub__unique_set(Key, Relation).
pub__unique_set_column(Key, Position, Column).
pub__non_null_column(Relation, Column).
pub__proved(Assertion).
pub__proof_key(Assertion, Key).
```

Public predicates need no export wrappers. Internal predicates connect the IR encoding to reasoning:

```prolog
relation_input(Filter, Input) :-
    ir__filter(Filter),
    ir__filter__input(Filter, Input).

direct_column_reference(Expression, Column) :-
    ir__column_ref(Expression),
    ir__column_ref__column(Expression, Column).
```

### Public Knowledge

Knowledge is a separate IR-linked type hierarchy. Its concrete types define the public facts the engine can receive:

```python
class Knowledge(metaclass=NodeMeta, abstract=True): ...

class NonNullColumn(Knowledge):
    relation: RelationExpr
    column: OutputColumn

class UniqueSet(Knowledge):
    relation: RelationExpr

class UniqueSetColumn(Knowledge):
    unique_set: UniqueSet
    position: int
    column: OutputColumn
```

The public encoder writes `pub__<knowledge class>` from those fields. SQL lowering constructs linked Knowledge directly. A database gatherer resolves qualified names before constructing the same objects; the engine never resolves relation or column names.

`facts.py` assigns solver identities and states IR structure and public Knowledge. Decisions such as which relation operations propagate Unique Sets or Non-Nullness belong in logic rules.

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
    ir.Node: lambda node, symbol: (),  # Do not reflect Node.origin
}
```

An exception replaces the default encoding for that class. Do not build a general customization framework until another concrete exception requires it.
