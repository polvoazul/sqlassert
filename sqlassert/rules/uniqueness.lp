% -----------------------------------------------------------------------------
% 0. Trivial Derivations for partially defined facts
% -----------------------------------------------------------------------------

pub__unique_set(UniqueSet) :- pub__unique_set__columns(UniqueSet, _).

% -----------------------------------------------------------------------------
% 1. Unique Sets established by an operation
%
% Grouping, DISTINCT, and partition qualification establish Unique Sets.
% -----------------------------------------------------------------------------

% established_unique_set(Relation) identifies the Unique Set established by that Relation Expression.
pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__aggregate__grouping_outputs(Relation, _, Column).

pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__distinct(Relation),
    ir__relation_expr__output_columns(Relation, _, Column).

pub__unique_set__columns(established_unique_set(Relation), Column) :-
    ir__qualify_by_partition__partition_outputs(Relation, _, Column).

% -----------------------------------------------------------------------------
% 2. Unique Sets preserved through an operation
%
% Carry Unique Sets through registered preserving operations when every Output
% Column in the Unique Set maps to exactly one Output Column.
% -----------------------------------------------------------------------------

% A RelationOperation derives a Unique Set from a SourceUniqueSet when:
% - RelationOperation preserves Unique Sets from the source Relation Expression.
% - SourceUniqueSet belongs to the source Relation Expression.
% - No Output Column in SourceUniqueSet is missing from the mapping.
% - No Output Column in SourceUniqueSet maps to multiple Output Columns.
pub__unique_set__columns(derived_unique_set(RelationOperation, SourceUniqueSet), TargetColumn) :-
    complete_unique_set_pass_through(RelationOperation, SourceUniqueSet),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    ir__relation_expr__output_columns(RelationOperation, _, TargetColumn),
    unique_pass_through_column(TargetColumn, SourceColumn).

% Every Output Column in the source Unique Set maps to exactly one Output Column.
complete_unique_set_pass_through(RelationOperation, SourceUniqueSet) :-
    unique_set_preserving_relation_operation(RelationOperation, Source),
    unique_set__relexp(SourceUniqueSet, Source),
    not missing_pass_through_column(RelationOperation, SourceUniqueSet, _),
    not ambiguous_pass_through_column(RelationOperation, SourceUniqueSet, _).

% Identify operations that preserve Unique Sets from a source Relation Expression.
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__alias__source(RelationOperation, Source).
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__named_relation__body(RelationOperation, Source).
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__filter__input(RelationOperation, Source).
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__project__input(RelationOperation, Source).
unique_set_preserving_relation_operation(RelationOperation, Source) :- ir__qualify_by_partition__input(RelationOperation, Source).

% A Unique Join preserves the Unique Sets of its left input.
unique_set_preserving_relation_operation(RelationOperation, Source) :-
    pub__unique_join__join(_UJ, RelationOperation), % this join is a Unique Join
    ir__join__left(RelationOperation, Source).

% An Output Column in the source Unique Set has no mapping that preserves distinct values.
missing_pass_through_column(RelationOperation, SourceUniqueSet, SourceColumn) :-
    unique_set_preserving_relation_operation(RelationOperation, Source),
    unique_set__relexp(SourceUniqueSet, Source),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    not target_has_pass_through_column(RelationOperation, SourceColumn).

% An Output Column in the source Unique Set maps to multiple Output Columns.
ambiguous_pass_through_column(RelationOperation, SourceUniqueSet, SourceColumn) :-
    unique_set_preserving_relation_operation(RelationOperation, Source),
    unique_set__relexp(SourceUniqueSet, Source),
    pub__unique_set__columns(SourceUniqueSet, SourceColumn),
    ir__relation_expr__output_columns(RelationOperation, _, FirstTargetColumn),
    ir__relation_expr__output_columns(RelationOperation, _, SecondTargetColumn),
    unique_pass_through_column(FirstTargetColumn, SourceColumn),
    unique_pass_through_column(SecondTargetColumn, SourceColumn),
    FirstTargetColumn != SecondTargetColumn.

% At least one Output Column preserves the source Output Column's distinct values.
target_has_pass_through_column(RelationOperation, SourceColumn) :-
    ir__relation_expr__output_columns(RelationOperation, _, TargetColumn),
    unique_pass_through_column(TargetColumn, SourceColumn).

% Map Output Columns through an injective Scalar Expression in an operation that preserves Unique Sets.
unique_pass_through_column(TargetColumn, SourceColumn) :-
    unique_set_preserving_relation_operation(RelationOperation, Source),
    ir__relation_expr__output_columns(Source, _, SourceColumn),
    ir__relation_expr__output_columns(RelationOperation, _, TargetColumn),
    ir__output_column__scalar_expr(TargetColumn, ScalarExpr),
    injective_scalar_expression(ScalarExpr, SourceColumn).

% Column References are injective Scalar Expressions; more operations can be added when injective.
injective_scalar_expression(ScalarExpr, SourceColumn) :-
    ir__column_ref__column(ScalarExpr, SourceColumn).

% -----------------------------------------------------------------------------
% 3. Unique Sets generalized to Column Sets of Interest
%
% Prove a Column Set of Interest is a Unique Set when it contains a known Unique Set.
% -----------------------------------------------------------------------------

% Assertions describe the sets under analysis.
% These are Column Sets of Interest. Only these sets receive the superset uniqueness implication.
% Other supersets are not generated as a performance optimization.
pub__unique_set__columns(ColumnSet, Column) :-
    pub__column_set_of_interest__columns(ColumnSet, Column),
    covers_unique_set(ColumnSet, _).

% -----------------------------------------------------------------------------
% 4. Helpers
%
% Check Unique Set containment for registered Column Set contexts, using
% Relation Expressions to select relevant Unique Sets.
% -----------------------------------------------------------------------------

% The Column Set contains every Output Column of another known Unique Set on the same Relation Expression.
covers_unique_set(Context, Key) :-
    candidate_unique_set(Context, Key),
    not missing_unique_set_member(Context, Key).

% At least one Output Column in the selected Unique Set is absent from the Column Set.
missing_unique_set_member(Context, Key) :-
    candidate_unique_set(Context, Key),
    pub__unique_set__columns(Key, Column),
    not column_set__columns(Context, Column).

% Select known Unique Sets on the Column Set's Relation Expression, excluding the Column Set itself.
candidate_unique_set(Context, Key) :-
    column_set__relexp(Context, Relation),
    unique_set__relexp(Key, Relation),
    Context != Key.

% Associate a Unique Set with a Relation Expression through an Output Column in the Unique Set.
unique_set__relexp(UniqueSet, Relation) :-
    pub__unique_set__columns(UniqueSet, Column),
    ir__relation_expr__output_columns(Relation, _, Column).

% Associate a Column Set of Interest with a Relation Expression through one of its Output Columns.
column_set__relexp(ColumnSet, Relation) :-  % one col set should have a single relation.
    pub__column_set_of_interest(ColumnSet),
    pub__column_set_of_interest__columns(ColumnSet, Column),
    ir__relation_expr__output_columns(Relation, _, Column).

% Expose the Output Columns of a Column Set of Interest for containment checks.
column_set__columns(ColumnSet, Column) :-
    pub__column_set_of_interest(ColumnSet),
    pub__column_set_of_interest__columns(ColumnSet, Column).
