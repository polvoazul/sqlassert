% Establish and apply Unique Sets.

% A Unique Set is a set of Output Columns. Relation membership is derived
% internally when a rule needs it.
pub__unique_set(UniqueSet) :- pub__unique_set__columns(UniqueSet, _).

unique_set_on_relation(UniqueSet, Relation) :-
    pub__unique_set__columns(UniqueSet, Column),
    ir__relation_expr__output_columns(Relation, _, Column).

% Relational operations that establish Unique Sets directly.
% The `established_unique_set(Relation)` is a key constructor for the Unique Set that this specific Relation Expression establishes.
pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__aggregate__grouping_outputs(Relation, _, Column).

pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__distinct(Relation),
    ir__relation_expr__output_columns(Relation, _, Column).

pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__qualify_by_partition__partition_outputs(Relation, _, Column).

% A Target derives a Unique Set from a SourceUniqueSet when:
% - Target is a Unique-Set-preserving relation operation over the source.
% - SourceUniqueSet belongs to that source.
% - No key member is missing.
% - No key member maps ambiguously.
pub__unique_set__columns(derived_unique_set(Target, SourceUniqueSet), TargetColumn) :-
    complete_unique_set_pass_through(Target, SourceUniqueSet),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    ir__relation_expr__output_columns(Target, _, TargetColumn),
    unique_pass_through_column(TargetColumn, SourceColumn).

complete_unique_set_pass_through(Target, SourceUniqueSet) :-
    unique_set_preserving_relation_operation(Target, Source),
    unique_set_on_relation(SourceUniqueSet, Source),
    not missing_pass_through_column(Target, SourceUniqueSet, _),
    not ambiguous_pass_through_column(Target, SourceUniqueSet, _).

unique_set_preserving_relation_operation(Target, Source) :- ir__alias__source(Target, Source).
unique_set_preserving_relation_operation(Target, Source) :- ir__named_relation__body(Target, Source).
unique_set_preserving_relation_operation(Target, Source) :- ir__filter__input(Target, Source).
unique_set_preserving_relation_operation(Target, Source) :- ir__project__input(Target, Source).
unique_set_preserving_relation_operation(Target, Source) :- ir__qualify_by_partition__input(Target, Source).
unique_set_preserving_relation_operation(Join, Left) :-
    pub__unique_join__join(_, Join),
    ir__join__left(Join, Left).

missing_pass_through_column(Target, SourceUniqueSet, SourceColumn) :-
    unique_set_preserving_relation_operation(Target, Source),
    unique_set_on_relation(SourceUniqueSet, Source),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    not target_has_pass_through_column(Target, SourceColumn).

% A source Unique Set member is ambiguous when it directly feeds more than one
% output column on the same Target, leaving no single column to carry forward.
ambiguous_pass_through_column(Target, SourceUniqueSet, SourceColumn) :-
    unique_set_preserving_relation_operation(Target, Source),
    unique_set_on_relation(SourceUniqueSet, Source),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    ir__relation_expr__output_columns(Target, _, FirstTargetColumn),
    ir__relation_expr__output_columns(Target, _, SecondTargetColumn),
    unique_pass_through_column(FirstTargetColumn, SourceColumn),
    unique_pass_through_column(SecondTargetColumn, SourceColumn),
    FirstTargetColumn != SecondTargetColumn.

target_has_pass_through_column(Target, SourceColumn) :-
    ir__relation_expr__output_columns(Target, _, TargetColumn),
    unique_pass_through_column(TargetColumn, SourceColumn).

% This marks columns that remain unique when they are unique in the source relation.
unique_pass_through_column(TargetColumn, SourceColumn) :-
    unique_set_preserving_relation_operation(Target, Source),
    column_direct_reference(Target, Source, TargetColumn, SourceColumn).

% A column is carried through without modification in a relation operation.
column_direct_reference(Target, Source, TargetColumn, SourceColumn) :-
    ir__relation_expr__output_columns(Source, _, SourceColumn),
    ir__relation_expr__output_columns(Target, _, TargetColumn),
    ir__output_column__scalar_expr(TargetColumn, ScalarExpr),
    ir__column_ref__column(ScalarExpr, SourceColumn).

% -----------------------------------------------------------------------------
% Generalize uniqueness to Column Sets of Interest
% -----------------------------------------------------------------------------

% Assertions describe the sets under analysis without declaring them unique.
% These are sets of interest. Only these sets receive the superset uniqueness implication.
% Other supersets are not generated as a performance optimization.
% This inference does not depend on assertion proof results.
pub__unique_set__columns(ColumnSet, Column) :-
    pub__column_set_of_interest__columns(ColumnSet, Column),
    pub__covers_unique_set(ColumnSet, _).

% -----------------------------------------------------------------------------
% Column Set Inputs
% -----------------------------------------------------------------------------

% Descriptive membership is separate from established unique-set membership.
% Join coverage uses internal Column Sets below, without marking them of interest.
column_set(ColumnSet, Relation) :-
    pub__column_set_of_interest(ColumnSet),
    pub__column_set_of_interest__columns(ColumnSet, Column),
    ir__relation_expr__output_columns(Relation, _, Column).

column_set_column(ColumnSet, Column) :-
    pub__column_set_of_interest(ColumnSet),
    pub__column_set_of_interest__columns(ColumnSet, Column).

% -----------------------------------------------------------------------------
% Generic Unique Set Coverage
% -----------------------------------------------------------------------------

candidate_unique_set(Context, Key) :-
    column_set(Context, Relation),
    unique_set_on_relation(Key, Relation),
    Context != Key.

pub__missing_unique_set_member(Context, Key, Column) :-
    candidate_unique_set(Context, Key),
    pub__unique_set__columns(Key, Column),
    not column_set_column(Context, Column).

pub__covers_unique_set(Context, Key) :-
    candidate_unique_set(Context, Key),
    not pub__missing_unique_set_member(Context, Key, _).

% -----------------------------------------------------------------------------
% Unique Joins
% -----------------------------------------------------------------------------

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

column_set(join_right_columns(Join), Right) :-
    supported_unique_join(Join),
    ir__join__right(Join, Right).

column_set_column(join_right_columns(Join), Column) :-
    supported_unique_join(Join),
    right_column_constrained(Join, Column).

% Publish the structural property independently of assertion queries.
pub__unique_join__join(established_unique_join(Join), Join) :-
    pub__covers_unique_set(join_right_columns(Join), _).

pub__unique_join(Property) :- pub__unique_join__join(Property, _).
