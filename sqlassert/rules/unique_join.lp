% -----------------------------------------------------------------------------
% 0. Trivial Derivations for partially defined facts
% -----------------------------------------------------------------------------

pub__unique_join(UniqueJoin) :- pub__unique_join__join(UniqueJoin, _).

% -----------------------------------------------------------------------------
% 1. Unique Joins
%
% A join preserves the uniqueness of its left rows when its equality conditions
% constrain right Output Columns that contain a Unique Set. Other AND-connected
% conditions can only remove right-side matches, so they do not affect this
% proof.
% -----------------------------------------------------------------------------

pub__unique_join__join(established_unique_join(Join), Join) :-
    supported_unique_join(Join),
    covers_unique_set(rhs_match_multiplicity_columns(Join), _).

% Limit Unique Join inference to inner and left joins.
supported_unique_join(Join) :- ir__join__kind(Join, "inner").
supported_unique_join(Join) :- ir__join__kind(Join, "left").

% `rhs_match_multiplicity_columns(Join)` is the internal Column Set containing
% the right Output Columns whose equality predicates determine how many right
% rows a single left row can match. The generic `column_set__*` predicates
% describe that set to the shared Unique Set coverage rules: `__relexp` ties a
% Column Set to its Relation Expression, and `__columns` enumerates its members.

% Associate the RHS match-multiplicity Column Set with the right relation.
column_set__relexp(rhs_match_multiplicity_columns(Join), Right) :-
    supported_unique_join(Join),
    ir__join__right(Join, Right).

% Expose the RHS columns constrained by equality for Unique Set coverage.
column_set__columns(rhs_match_multiplicity_columns(Join), Column) :-
    supported_unique_join(Join),
    rhs_match_multiplicity_column(Join, Column).

% Identify Output Columns belonging to the join's left input Relation Expression.
join__lhs_output_columns(Join, Column) :-
    ir__join__left(Join, Left),
    ir__relation_expr__output_columns(Left, _, Column).

% Identify Output Columns belonging to the join's right input Relation Expression.
join__rhs_output_columns(Join, Column) :-
    ir__join__right(Join, Right),
    ir__relation_expr__output_columns(Right, _, Column).

% Treat an equality's operands as an unordered pair so rules do not need one
% clause for each possible left/right spelling of the same predicate.
equality__operand(Equality, ScalarExpr) :-
    ir__equality__left(Equality, ScalarExpr).

equality__operand(Equality, ScalarExpr) :-
    ir__equality__right(Equality, ScalarExpr).

% A right Output Column affects match multiplicity when an equality constrains
% it to a value known for the current left row. Only these RHS columns can
% restrict the number of matching rows for that particular left row.
rhs_match_multiplicity_column(Join, Column) :-
    ir__join__equalities(Join, _, Equality),
    equality__operand(Equality, LeftScalarExpr),
    equality__operand(Equality, RightScalarExpr),
    LeftScalarExpr != RightScalarExpr,
    known_given_left_row(Join, LeftScalarExpr),
    ir__column_ref__column(RightScalarExpr, Column),
    join__rhs_output_columns(Join, Column).


% A scalar is known for a left row when it is a constant or a direct reference
% to a left Output Column. This matters because a Unique Set proves at most one
% RHS match only when every key column is fixed to one value for each LHS row;
% an unconstrained RHS expression cannot contribute to that proof.
known_given_left_row(Join, ScalarExpr) :-
    ir__join__equalities(Join, _, Equality),
    equality__operand(Equality, ScalarExpr),
    ir__constant(ScalarExpr).

known_given_left_row(Join, ScalarExpr) :-
    ir__column_ref__column(ScalarExpr, Column),
    join__lhs_output_columns(Join, Column).


