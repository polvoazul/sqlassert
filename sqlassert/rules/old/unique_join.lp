% Unique Join Assertions over relation expressions and output columns.

% Identifies every relation expression in the encoded program.
#defined relation_expression/1.
% Lists the output columns produced by a relation expression.
#defined relation_output_column/2.
% Declares a Unique Set belonging to a relation expression.
#defined unique_set/2.
% Lists the ordered output columns that belong to a Unique Set.
#defined unique_set_column/3.
% Records the SQL kind of a join relation expression.
#defined join_kind/2.
% Connects a join relation expression to its left input.
#defined join_left/2.
% Connects a join relation expression to its right input.
#defined join_right/2.
% Records one equality predicate from a join condition.
#defined join_equality/3.
% Connects an output column to the scalar expression that produces it.
#defined output_column_expression/2.
% Identifies an expression that directly references an upstream output column.
#defined direct_column_reference/2.
% Identifies an expression that selects an arbitrary value from an aggregate group.
#defined arbitrary_group_value/2.
% Identifies an expression whose value is a scalar constant.
#defined expression_constant/1.
% Identifies an expression whose semantics are unavailable to these rules.
#defined expression_opaque/1.
% Connects an assertion to the relation expression it constrains.
#defined assertion/2.
% Establishes that an output column cannot contain null.
#defined non_null_column/2.

% Lists the join kinds supported by the uniqueness proof.
supported_join_kind(inner).
supported_join_kind(left).

% Treats either side of a join equality as equivalent for later analysis.
equality_side(Join, Side, Other) :- join_equality(Join, Side, Other).
equality_side(Join, Other, Side) :- join_equality(Join, Side, Other).

% Recognizes scalar expressions whose equality behavior these rules understand.
understood(Expression) :- direct_column_reference(Expression, _).
understood(Expression) :- expression_constant(Expression).

% Determines whether an equality expression reads a column from the join's right input.
depends_on_right(Join, Expression) :-
    direct_column_reference(Expression, Column),
    join_right(Join, Right),
    relation_output_column(Right, Column).
% Determines whether the other side of a join equality is independent of the right input.
independent(Join, Expression) :-
    equality_side(Join, Expression, _),
    understood(Expression),
    not depends_on_right(Join, Expression).

% Determines whether a join equality covers a right-side output column.
covers(Join, Column) :-
    join_right(Join, Right),
    relation_output_column(Right, Column),
    equality_side(Join, Side, Other),
    direct_column_reference(Side, Column),
    independent(Join, Other).

% Selects each Unique Set known on a join's right input.
right_unique_set(Join, UniqueSet) :-
    join_right(Join, Right),
    unique_set(UniqueSet, Right).

% Identifies a right-side Unique Set column not covered by the join condition.
uncovered_member(Join, UniqueSet, Column) :-
    right_unique_set(Join, UniqueSet),
    unique_set_column(UniqueSet, _, Column),
    not covers(Join, Column).

% Records whether a Unique Set has at least one uncovered column.
has_uncovered_member(Join, UniqueSet) :- uncovered_member(Join, UniqueSet, _).
% Recognizes a right-side Unique Set fully covered by the join condition.
covered_unique_set(Join, UniqueSet) :-
    right_unique_set(Join, UniqueSet),
    not has_uncovered_member(Join, UniqueSet).

% Proves that a supported join cannot multiply rows from its left input.
unique_join(Join, UniqueSet) :-
    covered_unique_set(Join, UniqueSet),
    join_kind(Join, Kind),
    supported_join_kind(Kind).

% Marks an assertion as proved by any Unique Set that makes its join unique.
proved(Assertion) :- assertion(Assertion, Join), unique_join(Join, _).
% Records the Unique Set that proves an assertion.
proof_key(Assertion, UniqueSet) :- assertion(Assertion, Join), unique_join(Join, UniqueSet).

% Identifies a Unique Set that contains at least one nullable column.
not_candidate_key(UniqueSet) :-
    unique_set(UniqueSet, Relation),
    unique_set_column(UniqueSet, _, Column),
    not non_null_column(Relation, Column).
% Recognizes a Non-Null Unique Set.
candidate_key(UniqueSet) :- unique_set(UniqueSet, _), not not_candidate_key(UniqueSet).
% Marks an assertion as proved by a Non-Null Unique Set.
proved_by_candidate_key(Assertion) :- proof_key(Assertion, UniqueSet), candidate_key(UniqueSet).

% Counts the uncovered columns in a right-side Unique Set.
uncovered_count(Join, UniqueSet, Count) :-
    right_unique_set(Join, UniqueSet),
    Count = #count { Column : uncovered_member(Join, UniqueSet, Column) }.
% Finds the smallest uncovered-column count among the join's right-side Unique Sets.
closest_uncovered_count(Join, Count) :-
    uncovered_count(Join, _, _),
    Count = #min { OtherCount : uncovered_count(Join, _, OtherCount) }.
% Selects a right-side Unique Set with the fewest uncovered columns.
closest_unique_set(Join, UniqueSet) :-
    uncovered_count(Join, UniqueSet, Count),
    closest_uncovered_count(Join, Count).

% Reports an uncovered column from the closest candidate Unique Set for diagnostics.
assertion_missing_member(Assertion, UniqueSet, Column) :-
    assertion(Assertion, Join),
    closest_unique_set(Join, UniqueSet),
    uncovered_member(Join, UniqueSet, Column).

% Maps a left input column to a fresh join output column that directly passes through its value.
join_left_column(Join, InputColumn, OutputColumn) :-
    join_left(Join, Left),
    relation_output_column(Left, InputColumn),
    relation_output_column(Join, OutputColumn),
    output_column_expression(OutputColumn, Expression),
    direct_column_reference(Expression, InputColumn).

% Creates a derived Unique Set on a unique join from a Unique Set on its left input.
join_propagated_key(jk(SourceSet, Join), SourceSet, Join) :-
    join_left(Join, Left),
    unique_join(Join, _),
    unique_set(SourceSet, Left).

unique_set(JoinSet, Join) :- join_propagated_key(JoinSet, SourceSet, Join).
unique_set_column(JoinSet, Position, OutputColumn) :-
    join_propagated_key(JoinSet, SourceSet, Join),
    unique_set_column(SourceSet, Position, InputColumn),
    join_left_column(Join, InputColumn, OutputColumn).

non_null_column(Join, OutputColumn) :-
    join_left(Join, Left),
    unique_join(Join, _),
    non_null_column(Left, InputColumn),
    join_left_column(Join, InputColumn, OutputColumn).
