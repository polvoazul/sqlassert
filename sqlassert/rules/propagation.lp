% Uniqueness propagated through semantic column mappings.

#defined relation/1.
#defined relation_output/2.
#defined property_input/2.
#defined column_expression/2.
#defined expression_column/2.
#defined expression_any_aggregate/2.
#defined unique_set/2.
#defined unique_set_member/3.
#defined column_not_null/2.

mapped_column(OwnRelation, InputColumn, OutputColumn) :-
    property_input(OwnRelation, InputRelation),
    relation_output(InputRelation, InputColumn),
    relation_output(OwnRelation, OutputColumn),
    column_expression(OutputColumn, Expression),
    expression_column(Expression, InputColumn).

column_not_null(OwnRelation, OutputColumn) :-
    property_input(OwnRelation, InputRelation),
    column_not_null(InputRelation, InputColumn),
    mapped_column(OwnRelation, InputColumn, OutputColumn).

candidate_unique_set(SourceSet, OwnRelation) :-
    property_input(OwnRelation, InputRelation),
    unique_set(SourceSet, InputRelation).

uncovered_mapped_member(SourceSet, OwnRelation, InputColumn) :-
    candidate_unique_set(SourceSet, OwnRelation),
    unique_set_member(SourceSet, _, InputColumn),
    not mapped_column(OwnRelation, InputColumn, _).

propagated_key(pk(SourceSet, OwnRelation), SourceSet, OwnRelation) :-
    candidate_unique_set(SourceSet, OwnRelation),
    not uncovered_mapped_member(SourceSet, OwnRelation, _).

unique_set(PropagatedSet, OwnRelation) :-
    propagated_key(PropagatedSet, SourceSet, OwnRelation).
unique_set_member(PropagatedSet, Position, OutputColumn) :-
    propagated_key(PropagatedSet, SourceSet, OwnRelation),
    unique_set_member(SourceSet, Position, InputColumn),
    mapped_column(OwnRelation, InputColumn, OutputColumn).
