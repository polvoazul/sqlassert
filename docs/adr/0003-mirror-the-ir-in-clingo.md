# Mirror the IR in Clingo

## IR to logic encoding

The `ir_` namespace is the mirror model of the semantic IR inside the logic engine. Its vocabulary describes IR structure only; it does not state semantic conclusions that the logic engine should derive.

In Clingo, `ir_filter(f1)` is an atom, `ir_filter(f1).` is a ground fact, and `ir_filter/1` is the predicate signature. The encoder follows two predicate-name forms:

```text
ir_<class>
ir_<class>__<field>
```

### Node types and inheritance

Node types are unary predicates:

```prolog
ir_filter(f1).         % Filter
ir_column_ref(e1).     % ColumnRef
ir_output_column(c1).  % OutputColumn
```

The encoder states the concrete node type. Inheritance is part of the IR mirror but is encoded as logic rules instead of repeated ground facts:

```prolog
ir_relation_expr(Node) :- ir_filter(Node).
ir_relation_expr(Node) :- ir_project(Node).
ir_relation_expr(Node) :- ir_join(Node).

ir_scalar_expr(Node) :- ir_column_ref(Node).

ir_node(Node) :- ir_relation_expr(Node).
ir_node(Node) :- ir_scalar_expr(Node).
ir_node(Node) :- ir_output_column(Node).
```

These rules remain in the `ir_` namespace because they mechanically reproduce the Python type hierarchy. They do not interpret what those types mean for property reasoning.

### Fields

Scalar fields and object references put the owning object first and the field value second:

```prolog
ir_filter__input(f1, source1).                     % Filter.input
ir_output_column__name(c1, "user_id").             % OutputColumn.name
ir_output_column__expression(c1, e1).              % OutputColumn.expression
ir_column_ref__column(e1, source_column1).          % ColumnRef.column
ir_named_relation__role(r1, view).                 % NamedRelation.role
```

Ordered collections add their zero-based position before the member:

```prolog
ir_relation_expr__outputs(r1, 0, c1).  % RelationExpr.outputs[0]
ir_relation_expr__outputs(r1, 1, c2).  % RelationExpr.outputs[1]
```

Optional fields produce no fact when their value is `None`. Enum members are lowercase atoms such as `view` and `inner`; user-provided text is encoded as a string.

### Boolean fields

Boolean fields are unary predicates whose field names begin with `is_`. Presence means true and absence means false:

```prolog
ir_relation_expr__is_schema_complete(r1).
ir_unique_set_assertion__is_candidate_key(a1).
```

Rules that interpret absence bind the node through its type before using default negation:

```prolog
schema_incomplete(Relation) :-
    ir_relation_expr(Relation),
    not ir_relation_expr__is_schema_complete(Relation).
```

### Logical vocabulary

Predicates without the `ir_` prefix express semantic concepts derived by the logic engine. They are named for their domain meaning rather than for the Python representation:

```prolog
relation_input(Filter, Input) :-
    ir_filter(Filter),
    ir_filter__input(Filter, Input).

propagates_property(Filter, Input, unique_set) :-
    ir_filter(Filter),
    ir_filter__input(Filter, Input).

direct_column_reference(Expression, Column) :-
    ir_column_ref(Expression),
    ir_column_ref__column(Expression, Column).
```

This boundary keeps `facts.py` mechanical: it assigns solver identities and states IR structure. Decisions such as which relation operations propagate Unique Sets or Non-Nullness belong in the logic rules, where the specific property is explicit and independently derivable.

The coupling between the IR and its `ir_` vocabulary is intentional. Changing an IR class, field, or inheritance relationship requires changing its mirror encoding, while changes to semantic reasoning should normally affect only unprefixed logic predicates.
