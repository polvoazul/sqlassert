% -----------------------------------------------------------------------------
% 0. Trivial Derivations for partially defined facts
% -----------------------------------------------------------------------------

pub__unique_set(UniqueSet) :- pub__unique_set__columns(UniqueSet, _).
pub__unique_join(UniqueJoin) :- pub__unique_join__join(UniqueJoin, _).

% -----------------------------------------------------------------------------
% 1. Unique sets established by an operation
%
% Grouping, DISTINCT, and partition qualification establish unique sets.
% -----------------------------------------------------------------------------

% established_unique_set(Relation) identifies the set established by that relation.
pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__aggregate__grouping_outputs(Relation, _, Column).

pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__distinct(Relation),
    ir__relation_expr__output_columns(Relation, _, Column).

pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__qualify_by_partition__partition_outputs(Relation, _, Column).

% -----------------------------------------------------------------------------
% 2. Unique sets preserved through an operation
%
% Carry unique sets through aliases, filters, projections, and unique joins
% when every member maps to exactly one output column.
% -----------------------------------------------------------------------------

% A RelationOperation derives a Unique Set from a SourceUniqueSet when:
% - RelationOperation is a Unique-Set-preserving relation operation over the source.
% - SourceUniqueSet belongs to that source.
% - No key member is missing.
% - No key member maps ambiguously.
pub__unique_set__columns(derived_unique_set(RelationOperation, SourceUniqueSet), TargetColumn) :-
    complete_unique_set_pass_through(RelationOperation, SourceUniqueSet),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    ir__relation_expr__output_columns(RelationOperation, _, TargetColumn),
    unique_pass_through_column(TargetColumn, SourceColumn).

complete_unique_set_pass_through(RelationOperation, SourceUniqueSet) :-
    unique_set_preserving_relation_operation(RelationOperation, Source),
    unique_set_on_relation(SourceUniqueSet, Source),
    not missing_pass_through_column(RelationOperation, SourceUniqueSet, _),
    not ambiguous_pass_through_column(RelationOperation, SourceUniqueSet, _).

unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__alias__source(RelationOperation, Source).
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__named_relation__body(RelationOperation, Source).
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__filter__input(RelationOperation, Source).
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__project__input(RelationOperation, Source).
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__qualify_by_partition__input(RelationOperation, Source).
unique_set_preserving_relation_operation(Join, Left) :-
    pub__unique_join__join(_, Join),
    ir__join__left(Join, Left).

missing_pass_through_column(RelationOperation, SourceUniqueSet, SourceColumn) :-
    unique_set_preserving_relation_operation(RelationOperation, Source),
    unique_set_on_relation(SourceUniqueSet, Source),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    not target_has_pass_through_column(RelationOperation, SourceColumn).

% A source Unique Set member is ambiguous when it directly feeds more than one
% output column on the same Target, leaving no single column to carry forward.
ambiguous_pass_through_column(RelationOperation, SourceUniqueSet, SourceColumn) :-
    unique_set_preserving_relation_operation(RelationOperation, Source),
    unique_set_on_relation(SourceUniqueSet, Source),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    ir__relation_expr__output_columns(RelationOperation, _, FirstTargetColumn),
    ir__relation_expr__output_columns(RelationOperation, _, SecondTargetColumn),
    unique_pass_through_column(FirstTargetColumn, SourceColumn),
    unique_pass_through_column(SecondTargetColumn, SourceColumn),
    FirstTargetColumn != SecondTargetColumn.

target_has_pass_through_column(RelationOperation, SourceColumn) :-
    ir__relation_expr__output_columns(RelationOperation, _, TargetColumn),
    unique_pass_through_column(TargetColumn, SourceColumn).

% This marks columns that remain unique when they are unique in the source relation.
unique_pass_through_column(TargetColumn, SourceColumn) :-
    unique_set_preserving_relation_operation(RelationOperation, Source),
    ir__relation_expr__output_columns(Source, _, SourceColumn),
    ir__relation_expr__output_columns(RelationOperation, _, TargetColumn),
    ir__output_column__scalar_expr(TargetColumn, ScalarExpr),
    injective_scalar_expression(ScalarExpr, SourceColumn).

% Direct column references are injective. More operations can be added when
% their semantics guarantee that distinct input values produce distinct outputs.
injective_scalar_expression(ScalarExpr, SourceColumn) :-
    ir__column_ref__column(ScalarExpr, SourceColumn).

% -----------------------------------------------------------------------------
% 3. Unique sets generalized to sets of interest
%
% Prove an interesting set unique when it contains a known unique set.
% -----------------------------------------------------------------------------

% Assertions describe the sets under analysis.
% These are sets of interest. Only these sets receive the superset uniqueness implication.
% Other supersets are not generated as a performance optimization.
pub__unique_set__columns(ColumnSet, Column) :-
    pub__column_set_of_interest__columns(ColumnSet, Column),
    covers_unique_set(ColumnSet, _).

% -----------------------------------------------------------------------------
% 4. Unique joins
%
% Prove a join does not multiply left rows when its constrained right-side
% columns cover a unique set.
% -----------------------------------------------------------------------------

pub__unique_join__join(established_unique_join(Join), Join) :-
    covers_unique_set(join_right_columns(Join), _).

% A Join's constrained right-side columns are another arbitrary Column Set.
supported_unique_join(Join) :- ir__join__kind(Join, "inner").
supported_unique_join(Join) :- ir__join__kind(Join, "left").

join_left_output(Join, Column) :-
    ir__join__left(Join, Left),
    ir__relation_expr__output_columns(Left, _, Column).

join_right_output(Join, Column) :-
    ir__join__right(Join, Right),
    ir__relation_expr__output_columns(Right, _, Column).

understood_join_operand(Join, ScalarExpr) :-
    ir__constant(ScalarExpr),
    ir__join(Join).

understood_join_operand(Join, ScalarExpr) :-
    ir__column_ref__column(ScalarExpr, Column),
    join_left_output(Join, Column).

right_column_constrained(Join, Column) :-
    ir__join__equalities(Join, _, Equality),
    ir__equality__left(Equality, RightScalarExpr),
    ir__equality__right(Equality, OtherScalarExpr),
    ir__column_ref__column(RightScalarExpr, Column),
    join_right_output(Join, Column),
    understood_join_operand(Join, OtherScalarExpr).

right_column_constrained(Join, Column) :-
    ir__join__equalities(Join, _, Equality),
    ir__equality__right(Equality, RightScalarExpr),
    ir__equality__left(Equality, OtherScalarExpr),
    ir__column_ref__column(RightScalarExpr, Column),
    join_right_output(Join, Column),
    understood_join_operand(Join, OtherScalarExpr).

% -----------------------------------------------------------------------------
% 5. Helpers
%
% Share unique-set containment checks between sets of
% interest and joins, using relation membership to select relevant keys.
% -----------------------------------------------------------------------------

covers_unique_set(Context, Key) :-
    candidate_unique_set(Context, Key),
    not missing_unique_set_member(Context, Key).

missing_unique_set_member(Context, Key) :-
    candidate_unique_set(Context, Key),
    pub__unique_set__columns(Key, Column),
    not column_set_column(Context, Column).

candidate_unique_set(Context, Key) :-
    column_set(Context, Relation),
    unique_set_on_relation(Key, Relation),
    Context != Key.

unique_set_on_relation(UniqueSet, Relation) :-
    pub__unique_set__columns(UniqueSet, Column),
    ir__relation_expr__output_columns(Relation, _, Column).

% Descriptive membership is separate from established unique-set membership.
column_set(ColumnSet, Relation) :-
    pub__column_set_of_interest(ColumnSet),
    pub__column_set_of_interest__columns(ColumnSet, Column),
    ir__relation_expr__output_columns(Relation, _, Column).

column_set_column(ColumnSet, Column) :-
    pub__column_set_of_interest(ColumnSet),
    pub__column_set_of_interest__columns(ColumnSet, Column).

column_set(join_right_columns(Join), Right) :-
    supported_unique_join(Join),
    ir__join__right(Join, Right).

column_set_column(join_right_columns(Join), Column) :-
    supported_unique_join(Join),
    right_column_constrained(Join, Column).
