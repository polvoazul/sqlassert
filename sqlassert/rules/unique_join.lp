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

%% TO DOUBLE CHECK BELOW THIS LINE

% Associate the right-side match-multiplicity columns with the right relation.
column_set(rhs_match_multiplicity_columns(Join), Right) :-
    supported_unique_join(Join),
    ir__join__right(Join, Right).

% Expose right Output Columns constrained by equality for key coverage checks.
column_set_column(rhs_match_multiplicity_columns(Join), Column) :-
    supported_unique_join(Join),
    rhs_match_multiplicity_column(Join, Column).

% Identify Output Columns belonging to the join's left input Relation Expression.
join_lhs_output_columns(Join, Column) :-
    ir__join__left(Join, Left),
    ir__relation_expr__output_columns(Left, _, Column).

% Identify Output Columns belonging to the join's right input Relation Expression.
join_rhs_output_columns(Join, Column) :-
    ir__join__right(Join, Right),
    ir__relation_expr__output_columns(Right, _, Column).

% A scalar is known for a left row when it is a constant used by this join or
% a direct reference to a left Output Column.
known_given_left_row(Join, ScalarExpr) :-
    ir__join__equalities(Join, _, Equality),
    ir__equality__left(Equality, ScalarExpr),
    ir__constant(ScalarExpr).

known_given_left_row(Join, ScalarExpr) :-
    ir__join__equalities(Join, _, Equality),
    ir__equality__right(Equality, ScalarExpr),
    ir__constant(ScalarExpr).

known_given_left_row(Join, ScalarExpr) :-
    ir__column_ref__column(ScalarExpr, Column),
    join_left_output(Join, Column).

% A right Output Column affects match multiplicity when an equality constrains
% it to a value known for the current left row.
rhs_match_multiplicity_column(Join, Column) :-
    ir__join__equalities(Join, _, Equality),
    ir__equality__left(Equality, RightScalarExpr),
    ir__equality__right(Equality, OtherScalarExpr),
    ir__column_ref__column(RightScalarExpr, Column),
    join_right_output(Join, Column),
    known_given_left_row(Join, OtherScalarExpr).

rhs_match_multiplicity_column(Join, Column) :-
    ir__join__equalities(Join, _, Equality),
    ir__equality__right(Equality, RightScalarExpr),
    ir__equality__left(Equality, OtherScalarExpr),
    ir__column_ref__column(RightScalarExpr, Column),
    join_right_output(Join, Column),
    known_given_left_row(Join, OtherScalarExpr).


% A Unique Join preserves the Unique Sets of its left input.
unique_set_preserving_relation_operation(Join, Left) :-
    pub__unique_join__join(_, Join),
    ir__join__left(Join, Left).
