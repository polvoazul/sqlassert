% Establish and apply Unique Sets.

% A Unique Set is only a list of Output Columns. Relation membership is derived
% internally when a rule needs it.
pub__unique_set(Key) :- pub__unique_set__columns(Key, _).

unique_set_on_relation(Key, Relation) :-
    pub__unique_set(Key),
    pub__unique_set__columns(Key, Column),
    ir__relation_expr__output_columns(Relation, _, Column).

% Relational operations that establish Unique Sets directly.
pub__unique_set__columns(aggregate_unique_set(Relation), Column) :-
    ir__aggregate__grouping_outputs(Relation, _, Column).

pub__unique_set__columns(distinct_unique_set(Relation), Column) :-
    ir__distinct(Relation),
    ir__relation_expr__output_columns(Relation, _, Column).

pub__unique_set__columns(partition_unique_set(Relation), Column) :-
    ir__qualify_by_partition__partition_outputs(Relation, _, Column).

% Property-preserving operations and their fresh Pass-Through Columns.
property_preserving_input(Target, Source) :- ir__alias__source(Target, Source).
property_preserving_input(Target, Source) :- ir__named_relation__body(Target, Source).
property_preserving_input(Target, Source) :- ir__filter__input(Target, Source).
property_preserving_input(Target, Source) :- ir__project__input(Target, Source).
property_preserving_input(Target, Source) :- ir__qualify_by_partition__input(Target, Source).
property_preserving_input(Join, Left) :-
    unique_join(Join, _),
    ir__join__left(Join, Left).

pass_through_column(Target, SourceColumn, TargetColumn) :-
    property_preserving_input(Target, Source),
    ir__relation_expr__output_columns(Source, _, SourceColumn),
    ir__relation_expr__output_columns(Target, _, TargetColumn),
    ir__output_column__expression(TargetColumn, Expression),
    ir__column_ref__column(Expression, SourceColumn).

missing_pass_through_column(Target, SourceKey, SourceColumn) :-
    property_preserving_input(Target, Source),
    unique_set_on_relation(SourceKey, Source),
    pub__unique_set__columns(SourceKey, SourceColumn),
    not pass_through_column(Target, SourceColumn, _).

ambiguous_pass_through_column(Target, SourceKey, SourceColumn) :-
    property_preserving_input(Target, Source),
    unique_set_on_relation(SourceKey, Source),
    pub__unique_set__columns(SourceKey, SourceColumn),
    pass_through_column(Target, SourceColumn, FirstTargetColumn),
    pass_through_column(Target, SourceColumn, SecondTargetColumn),
    FirstTargetColumn != SecondTargetColumn.

complete_unique_set_pass_through(Target, SourceKey) :-
    property_preserving_input(Target, Source),
    unique_set_on_relation(SourceKey, Source),
    not missing_pass_through_column(Target, SourceKey, _),
    not ambiguous_pass_through_column(Target, SourceKey, _).

pub__unique_set__columns(derived_unique_set(Target, SourceKey), TargetColumn) :-
    complete_unique_set_pass_through(Target, SourceKey),
    pub__unique_set__columns(SourceKey, SourceColumn),
    pass_through_column(Target, SourceColumn, TargetColumn).

% A proved ordinary assertion contributes its asserted columns as a Unique Set
% for later Relation Expressions. Positive derivation prevents self-proof.
pub__unique_set__columns(asserted_unique_set(Assertion), Column) :-
    pub__proved(Assertion),
    ir__unique_set_assertion__columns(Assertion, _, Column),
    not ir__unique_set_assertion__is_candidate_key(Assertion).

% Generic arbitrary-column-set containment.
column_set(asserted_columns(Assertion), Relation) :-
    ir__unique_set_assertion__subject(Assertion, Relation),
    not ir__unique_set_assertion__is_candidate_key(Assertion).

column_set_column(asserted_columns(Assertion), Column) :-
    ir__unique_set_assertion__columns(Assertion, _, Column),
    not ir__unique_set_assertion__is_candidate_key(Assertion).

candidate_unique_set(Context, Key) :-
    column_set(Context, Relation),
    unique_set_on_relation(Key, Relation),
    not excluded_unique_set(Context, Key).

excluded_unique_set(asserted_columns(Assertion), asserted_unique_set(Assertion)) :-
    ir__unique_set_assertion(Assertion).

missing_unique_set_member(Context, Key, Column) :-
    candidate_unique_set(Context, Key),
    pub__unique_set__columns(Key, Column),
    not column_set_column(Context, Column).

covers_unique_set(Context, Key) :-
    candidate_unique_set(Context, Key),
    not missing_unique_set_member(Context, Key, _).

pub__proved(Assertion) :-
    ir__unique_set_assertion(Assertion),
    covers_unique_set(asserted_columns(Assertion), _).

pub__proof_key(Assertion, Key) :-
    ir__unique_set_assertion(Assertion),
    covers_unique_set(asserted_columns(Assertion), Key).

pub__assertion_missing_member(Assertion, Key, Column) :-
    ir__unique_set_assertion(Assertion),
    missing_unique_set_member(asserted_columns(Assertion), Key, Column).

% A Join's constrained right-side columns are another arbitrary column set.
supported_unique_join(Join) :- ir__join__kind(Join, "inner").
supported_unique_join(Join) :- ir__join__kind(Join, "left").

join_left_output(Join, Column) :-
    ir__join__left(Join, Left),
    ir__relation_expr__output_columns(Left, _, Column).

join_right_output(Join, Column) :-
    ir__join__right(Join, Right),
    ir__relation_expr__output_columns(Right, _, Column).

understood_join_operand(Join, Expression) :-
    ir__constant(Expression),
    ir__join(Join).

understood_join_operand(Join, Expression) :-
    ir__column_ref__column(Expression, Column),
    join_left_output(Join, Column).

right_column_constrained(Join, Column) :-
    ir__join__equalities(Join, _, Equality),
    ir__equality__left(Equality, RightExpression),
    ir__equality__right(Equality, OtherExpression),
    ir__column_ref__column(RightExpression, Column),
    join_right_output(Join, Column),
    understood_join_operand(Join, OtherExpression).

right_column_constrained(Join, Column) :-
    ir__join__equalities(Join, _, Equality),
    ir__equality__right(Equality, RightExpression),
    ir__equality__left(Equality, OtherExpression),
    ir__column_ref__column(RightExpression, Column),
    join_right_output(Join, Column),
    understood_join_operand(Join, OtherExpression).

column_set(join_right_columns(Join), Right) :-
    supported_unique_join(Join),
    ir__join__right(Join, Right).

column_set_column(join_right_columns(Join), Column) :-
    supported_unique_join(Join),
    right_column_constrained(Join, Column).

unique_join(Join, Key) :-
    covers_unique_set(join_right_columns(Join), Key).

pub__proved(Assertion) :-
    ir__unique_join_assertion__subject(Assertion, Join),
    unique_join(Join, _).

pub__proof_key(Assertion, Key) :-
    ir__unique_join_assertion__subject(Assertion, Join),
    unique_join(Join, Key).

pub__assertion_missing_member(Assertion, Key, Column) :-
    ir__unique_join_assertion__subject(Assertion, Join),
    missing_unique_set_member(join_right_columns(Join), Key, Column).
