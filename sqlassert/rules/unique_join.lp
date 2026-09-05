% -----------------------------------------------------------------------------
% 0. Trivial Derivations for partially defined facts
% -----------------------------------------------------------------------------

pub__unique_join(UniqueJoin) :- pub__unique_join__join(UniqueJoin, _).

% -----------------------------------------------------------------------------
% 1. Unique Joins
%
% Prove a join does not multiply left rows when its constrained right-side
% Output Columns contain a Unique Set.
% -----------------------------------------------------------------------------

pub__unique_join__join(established_unique_join(Join), Join) :-
    covers_unique_set(join_right_columns(Join), _).

% Limit Unique Join inference to inner and left joins.
supported_unique_join(Join) :- ir__join__kind(Join, "inner").
supported_unique_join(Join) :- ir__join__kind(Join, "left").

% Identify Output Columns belonging to the join's left input Relation Expression.
join_left_output(Join, Column) :-
    ir__join__left(Join, Left),
    ir__relation_expr__output_columns(Left, _, Column).

% Identify Output Columns belonging to the join's right input Relation Expression.
join_right_output(Join, Column) :-
    ir__join__right(Join, Right),
    ir__relation_expr__output_columns(Right, _, Column).

% Constants and Column References to the left input have a fixed value for each left row.
understood_join_operand(Join, ScalarExpr) :-
    ir__constant(ScalarExpr),
    ir__join(Join).

understood_join_operand(Join, ScalarExpr) :-
    ir__column_ref__column(ScalarExpr, Column),
    join_left_output(Join, Column).

% An Equality fixes a right Output Column to a Constant or a Column Reference to the left input.
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

% Associate the Column Set of constrained right Output Columns with the right input Relation Expression.
column_set(join_right_columns(Join), Right) :-
    supported_unique_join(Join),
    ir__join__right(Join, Right).

% Expose constrained right Output Columns as Column Set members for containment checks.
column_set_column(join_right_columns(Join), Column) :-
    supported_unique_join(Join),
    right_column_constrained(Join, Column).
