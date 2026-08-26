% Unique Join Assertions over relation expressions and output columns.

#defined relation/1.
#defined relation_output/2.
#defined unique_set/2.
#defined unique_set_member/3.
#defined join_kind/2.
#defined join_left/2.
#defined join_right/2.
#defined join_equality/3.
#defined column_expression/2.
#defined expression_column/2.
#defined expression_any_aggregate/2.
#defined expression_constant/1.
#defined expression_opaque/1.
#defined assertion/2.
#defined column_not_null/2.

supported_join_kind(inner).
supported_join_kind(left).

equality_side(Join, Side, Other) :- join_equality(Join, Side, Other).
equality_side(Join, Other, Side) :- join_equality(Join, Side, Other).

understood(Expression) :- expression_column(Expression, _).
understood(Expression) :- expression_constant(Expression).

depends_on_right(Join, Expression) :-
    expression_column(Expression, Column),
    join_right(Join, Right),
    relation_output(Right, Column).
independent(Join, Expression) :-
    equality_side(Join, Expression, _),
    understood(Expression),
    not depends_on_right(Join, Expression).

covers(Join, Column) :-
    join_right(Join, Right),
    relation_output(Right, Column),
    equality_side(Join, Side, Other),
    expression_column(Side, Column),
    independent(Join, Other).

right_unique_set(Join, UniqueSet) :-
    join_right(Join, Right),
    unique_set(UniqueSet, Right).

uncovered_member(Join, UniqueSet, Column) :-
    right_unique_set(Join, UniqueSet),
    unique_set_member(UniqueSet, _, Column),
    not covers(Join, Column).

has_uncovered_member(Join, UniqueSet) :- uncovered_member(Join, UniqueSet, _).
covered_unique_set(Join, UniqueSet) :-
    right_unique_set(Join, UniqueSet),
    not has_uncovered_member(Join, UniqueSet).

unique_join(Join, UniqueSet) :-
    covered_unique_set(Join, UniqueSet),
    join_kind(Join, Kind),
    supported_join_kind(Kind).

proved(Assertion) :- assertion(Assertion, Join), unique_join(Join, _).
proof_key(Assertion, UniqueSet) :- assertion(Assertion, Join), unique_join(Join, UniqueSet).

not_candidate_key(UniqueSet) :-
    unique_set(UniqueSet, Relation),
    unique_set_member(UniqueSet, _, Column),
    not column_not_null(Relation, Column).
candidate_key(UniqueSet) :- unique_set(UniqueSet, _), not not_candidate_key(UniqueSet).
proved_by_candidate_key(Assertion) :- proof_key(Assertion, UniqueSet), candidate_key(UniqueSet).

uncovered_count(Join, UniqueSet, Count) :-
    right_unique_set(Join, UniqueSet),
    Count = #count { Column : uncovered_member(Join, UniqueSet, Column) }.
closest_uncovered_count(Join, Count) :-
    uncovered_count(Join, _, _),
    Count = #min { OtherCount : uncovered_count(Join, _, OtherCount) }.
closest_unique_set(Join, UniqueSet) :-
    uncovered_count(Join, UniqueSet, Count),
    closest_uncovered_count(Join, Count).

assertion_missing_member(Assertion, UniqueSet, Column) :-
    assertion(Assertion, Join),
    closest_unique_set(Join, UniqueSet),
    uncovered_member(Join, UniqueSet, Column).

% A proven Unique Join preserves the left relation's properties on fresh join
% output columns. Output columns already carry the direct structural mapping.
join_left_column(Join, InputColumn, OutputColumn) :-
    join_left(Join, Left),
    relation_output(Left, InputColumn),
    relation_output(Join, OutputColumn),
    column_expression(OutputColumn, Expression),
    expression_column(Expression, InputColumn).

join_propagated_key(jk(SourceSet, Join), SourceSet, Join) :-
    join_left(Join, Left),
    unique_join(Join, _),
    unique_set(SourceSet, Left).

unique_set(JoinSet, Join) :- join_propagated_key(JoinSet, SourceSet, Join).
unique_set_member(JoinSet, Position, OutputColumn) :-
    join_propagated_key(JoinSet, SourceSet, Join),
    unique_set_member(SourceSet, Position, InputColumn),
    join_left_column(Join, InputColumn, OutputColumn).

column_not_null(Join, OutputColumn) :-
    join_left(Join, Left),
    unique_join(Join, _),
    column_not_null(Left, InputColumn),
    join_left_column(Join, InputColumn, OutputColumn).
