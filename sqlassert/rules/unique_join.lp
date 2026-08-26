% Unique Join Assertions.
%
% A Unique Join is proved when every member of some Unique Set of the join's
% right-hand Relation Instance is determined by the join predicate: equated
% to a column from outside the join's right-hand side, or to a constant.
% Absence of such a proof is UNKNOWN, never a Disproof.
%
% INNER and LEFT joins share this reasoning: covering the right-hand Unique
% Set bounds matches to at most one row per left-hand row either way, whether
% or not an unmatched left row goes on to carry NULLs.
%
% When no Unique Set is fully covered, the Unique Set(s) missing the fewest
% members are surfaced as best-effort evidence of what is missing.
%
% Deterministic and stratified by policy: facts, normal derivation, stratified
% default negation, and deterministic aggregates. See docs/clingo-rule-policy.md.
%
% Variable naming used in this file: JoinId = join id, UniqueSetKey = Unique
% Set key, InstanceId = Relation Instance id, DefinitionId = Relation
% Definition id, OutputDefinitionId = a join's own output Relation Definition
% id, ColumnName = column name, ExpressionId = expression id, AssertionId =
% assertion id, Count/OtherCount = counts. Side/Other/Kind spell themselves
% out because there's no established one-letter form for them.

% The fact interface the property engine grounds against. Declared so that a
% program legitimately lacking any of these facts is not an undefined-atom
% warning.
#defined relation/1.
#defined unique_set/2.
#defined unique_set_member/3.
#defined instance_of/2.
#defined join_kind/2.
#defined join_left_instance/2.
#defined join_right_instance/2.
#defined join_equality/3.
#defined expression_column/3.
#defined expression_constant/1.
#defined expression_opaque/1.
#defined assertion/2.
#defined column_not_null/2.
#defined join_output_definition/2.

% The join kinds whose row multiplication this policy can reason about.
supported_join_kind(inner).
supported_join_kind(left).

% An equality constrains both of its sides.
equality_side(JoinId, Side, Other) :- join_equality(JoinId, Side, Other).
equality_side(JoinId, Other, Side) :- join_equality(JoinId, Side, Other).

% Only expressions conversion actually modelled can determine a value.
% Opaque expressions deliberately determine nothing.
understood(ExpressionId) :- expression_column(ExpressionId, _, _).
understood(ExpressionId) :- expression_constant(ExpressionId).

depends_on_right(JoinId, ExpressionId) :-
    expression_column(ExpressionId, InstanceId, _),
    join_right_instance(JoinId, InstanceId).
independent(JoinId, ExpressionId) :-
    equality_side(JoinId, ExpressionId, _),
    understood(ExpressionId),
    not depends_on_right(JoinId, ExpressionId).

% A right-hand column is covered when equated to an expression that does not
% depend on the right-hand side of the same join -- a column from elsewhere in
% the query, or a constant.
covers(JoinId, InstanceId, ColumnName) :-
    join_right_instance(JoinId, InstanceId),
    equality_side(JoinId, Side, Other),
    expression_column(Side, InstanceId, ColumnName),
    independent(JoinId, Other).

% A Unique Set of the join's right-hand Relation Instance.
right_unique_set(JoinId, UniqueSetKey) :-
    join_right_instance(JoinId, InstanceId),
    instance_of(InstanceId, DefinitionId),
    unique_set(UniqueSetKey, DefinitionId).

uncovered_member(JoinId, UniqueSetKey, ColumnName) :-
    right_unique_set(JoinId, UniqueSetKey),
    join_right_instance(JoinId, InstanceId),
    instance_of(InstanceId, DefinitionId),
    unique_set(UniqueSetKey, DefinitionId),
    unique_set_member(UniqueSetKey, _, ColumnName),
    not covers(JoinId, InstanceId, ColumnName).

has_uncovered_member(JoinId, UniqueSetKey) :- uncovered_member(JoinId, UniqueSetKey, _).
covered_unique_set(JoinId, UniqueSetKey) :-
    right_unique_set(JoinId, UniqueSetKey),
    not has_uncovered_member(JoinId, UniqueSetKey).

unique_join(JoinId, UniqueSetKey) :-
    covered_unique_set(JoinId, UniqueSetKey),
    join_kind(JoinId, Kind),
    supported_join_kind(Kind).

proved(AssertionId) :- assertion(AssertionId, JoinId), unique_join(JoinId, _).
proof_key(AssertionId, UniqueSetKey) :-
    assertion(AssertionId, JoinId),
    unique_join(JoinId, UniqueSetKey).

% A Candidate Key is a Unique Set whose every member is known non-null. This
% is only ever additional evidence about a proof already made above: it never
% affects whether a Unique Set counts as covered, since a nullable Unique Set
% already suffices for ordinary equality-join uniqueness. See the Null
% Semantics section of docs/mvp-scope.md.
not_candidate_key(UniqueSetKey) :-
    unique_set(UniqueSetKey, DefinitionId),
    unique_set_member(UniqueSetKey, _, ColumnName),
    not column_not_null(DefinitionId, ColumnName).
candidate_key(UniqueSetKey) :- unique_set(UniqueSetKey, _), not not_candidate_key(UniqueSetKey).

proved_by_candidate_key(AssertionId) :- proof_key(AssertionId, UniqueSetKey), candidate_key(UniqueSetKey).

% Best-effort evidence for UNKNOWN: among the right-hand relation's Unique
% Sets, the one(s) missing the fewest members, and which members those are.
% A proved assertion has a fully covered Unique Set, so this never disagrees
% with a proof; it only ever fires for the Unique Sets that fell short.
uncovered_count(JoinId, UniqueSetKey, Count) :-
    right_unique_set(JoinId, UniqueSetKey),
    Count = #count { ColumnName : uncovered_member(JoinId, UniqueSetKey, ColumnName) }.
closest_uncovered_count(JoinId, Count) :-
    uncovered_count(JoinId, _, _),
    Count = #min { OtherCount : uncovered_count(JoinId, _, OtherCount) }.
closest_unique_set(JoinId, UniqueSetKey) :-
    uncovered_count(JoinId, UniqueSetKey, Count),
    closest_uncovered_count(JoinId, Count).

assertion_missing_member(AssertionId, UniqueSetKey, ColumnName) :-
    assertion(AssertionId, JoinId),
    closest_unique_set(JoinId, UniqueSetKey),
    uncovered_member(JoinId, UniqueSetKey, ColumnName).

% A join structurally known not to multiply its left-hand input's rows --
% unique_join/2 above, with or without an assertion on it -- passes every one
% of the left side's own Unique Sets, and their non-null status, through to
% the join's own output unrenamed: exactly the reasoning the Unique Join
% Assertion's own definition already states ("a join that cannot increase the
% number of rows contributed by its left-hand input"), so anything unique on
% the left cannot have become non-unique on the output either. The right
% side's own Unique Sets do not transfer: many left rows are allowed to share
% one right row, which is exactly what "unique from the left" permits.
unique_set(UniqueSetKey, OutputDefinitionId) :-
    join_left_instance(JoinId, InstanceId),
    unique_join(JoinId, _),
    instance_of(InstanceId, DefinitionId),
    unique_set(UniqueSetKey, DefinitionId),
    join_output_definition(JoinId, OutputDefinitionId).

column_not_null(OutputDefinitionId, ColumnName) :-
    join_left_instance(JoinId, InstanceId),
    unique_join(JoinId, _),
    instance_of(InstanceId, DefinitionId),
    column_not_null(DefinitionId, ColumnName),
    join_output_definition(JoinId, OutputDefinitionId).

% There is no `unknown` rule: UNKNOWN is precisely the absence of a proof, and
% the reporter reads it that way.
