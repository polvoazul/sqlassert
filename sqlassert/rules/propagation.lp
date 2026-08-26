% Uniqueness propagated through Filter and Project.
%
% A Filter's own Relation Definition inherits every Unique Set of its input
% relation whole and unchanged: a Filter can only remove rows, and neither
% renames nor drops a column, so no member-coverage check is needed the way
% Project's is -- the source Unique Set's own key and member facts already
% describe the columns Filter's Definition has too.
%
% A Project's own Relation Definition earns a new Unique Set for every Unique
% Set of its input relation that survives the projection whole: every member
% column must appear, unrenamed-computation, among the projected outputs. The
% new Unique Set's members are the *output* names, so a later join against the
% projected alias sees the columns it actually has.
%
% Deterministic and stratified by policy: facts and normal derivation only, one
% level of default negation over facts already derived from base facts alone.
% See docs/clingo-rule-policy.md.
%
% Variable naming used in this file: OwnDefinitionId = a Filter or Project's
% own Relation Definition id, InstanceId = Relation Instance id, DefinitionId
% = Relation Definition id, ColumnName = an input-side column name,
% OutputColumn = the corresponding output column name, ExpressionId =
% expression id, SourceSetKey = a source Unique Set key, PropagatedSetKey =
% the Unique Set key Project's propagation derives.

#defined filter_input/2.
#defined project_input/2.
#defined project_output/3.
#defined join_left_instance/2.
#defined join_output_definition/2.

% A Project's `project_input` names the single instance it reads from -- the
% Join's own output instance, when its input is a Join, so that Join's
% uniqueness-propagation stays gated by `unique_join/2` (see
% `rules/unique_join.lp`) rather than trusting either side's raw Knowledge
% directly. But a projected column's own expression still names whichever
% *true* child instance it was resolved against during lowering (`sessions`
% or `users`, never the Join's own synthetic one) -- column naming is settled
% once, before any fact is generated. This bridges the two: a column drawn
% from a Join's left instance is also recognised as belonging to the Join's
% own output instance, so `projected_column` below can match it. Never
% bridged from the right: the right side's own columns are not what a
% unique_join proof is about (see `rules/unique_join.lp`'s own comment).
resolves_via_join_output(TrueInstanceId, OutputInstanceId) :-
    join_left_instance(JoinId, TrueInstanceId),
    join_output_definition(JoinId, OutputDefinitionId),
    instance_of(OutputInstanceId, OutputDefinitionId).

unique_set(SourceSetKey, OwnDefinitionId) :-
    filter_input(OwnDefinitionId, InstanceId),
    instance_of(InstanceId, DefinitionId),
    unique_set(SourceSetKey, DefinitionId).
column_not_null(OwnDefinitionId, ColumnName) :-
    filter_input(OwnDefinitionId, InstanceId),
    instance_of(InstanceId, DefinitionId),
    column_not_null(DefinitionId, ColumnName).

% An output column whose source expression is exactly one member column of the
% Project's input relation, unrenamed-computation.
projected_column(OwnDefinitionId, ColumnName, OutputColumn) :-
    project_output(OwnDefinitionId, OutputColumn, ExpressionId),
    project_input(OwnDefinitionId, InstanceId),
    expression_column(ExpressionId, InstanceId, ColumnName).
projected_column(OwnDefinitionId, ColumnName, OutputColumn) :-
    project_output(OwnDefinitionId, OutputColumn, ExpressionId),
    project_input(OwnDefinitionId, InstanceId),
    resolves_via_join_output(TrueInstanceId, InstanceId),
    expression_column(ExpressionId, TrueInstanceId, ColumnName).

% An output column computed unrenamed from one non-null input column is
% itself non-null, the same way Filter's `column_not_null` above carries its
% input's whole unchanged -- otherwise a Candidate Key proved through a
% renaming or reordering Project would wrongly read as merely nullable.
column_not_null(OwnDefinitionId, OutputColumn) :-
    projected_column(OwnDefinitionId, ColumnName, OutputColumn),
    project_input(OwnDefinitionId, InstanceId),
    instance_of(InstanceId, DefinitionId),
    column_not_null(DefinitionId, ColumnName).

% A Unique Set of the relation a Project draws from -- a candidate for
% propagation, pending a check that every member survived the projection.
candidate_unique_set(SourceSetKey, OwnDefinitionId) :-
    project_input(OwnDefinitionId, InstanceId),
    instance_of(InstanceId, DefinitionId),
    unique_set(SourceSetKey, DefinitionId).

uncovered_projected_member(SourceSetKey, OwnDefinitionId, ColumnName) :-
    candidate_unique_set(SourceSetKey, OwnDefinitionId),
    unique_set_member(SourceSetKey, _, ColumnName),
    not projected_column(OwnDefinitionId, ColumnName, _).

% `pk(SourceSetKey, OwnDefinitionId)` is a fresh compound identifier for the
% Unique Set Project OwnDefinitionId earns from source Unique Set
% SourceSetKey -- safe to invent since both are already ground.
propagated_key(pk(SourceSetKey, OwnDefinitionId), SourceSetKey, OwnDefinitionId) :-
    candidate_unique_set(SourceSetKey, OwnDefinitionId),
    not uncovered_projected_member(SourceSetKey, OwnDefinitionId, _).

unique_set(PropagatedSetKey, OwnDefinitionId) :-
    propagated_key(PropagatedSetKey, SourceSetKey, OwnDefinitionId).
unique_set_member(PropagatedSetKey, Position, OutputColumn) :-
    propagated_key(PropagatedSetKey, SourceSetKey, OwnDefinitionId),
    unique_set_member(SourceSetKey, Position, ColumnName),
    projected_column(OwnDefinitionId, ColumnName, OutputColumn).
