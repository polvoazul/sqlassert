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
% Variable naming used in this file: J = join id, K = Unique Set key,
% I = Relation Instance id, R = Relation Definition id, C = column name,
% E = expression id, A = assertion id, N/M = counts. Side/Other/Kind spell
% themselves out because there's no established one-letter form for them.

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

% The join kinds whose row multiplication this policy can reason about.
supported_join_kind(inner).
supported_join_kind(left).

% An equality constrains both of its sides.
equality_side(J, Side, Other) :- join_equality(J, Side, Other).
equality_side(J, Other, Side) :- join_equality(J, Side, Other).

% Only expressions conversion actually modelled can determine a value.
% Opaque expressions deliberately determine nothing.
understood(E) :- expression_column(E, _, _).
understood(E) :- expression_constant(E).

depends_on_right(J, E) :- expression_column(E, I, _), join_right_instance(J, I).
independent(J, E) :- equality_side(J, E, _), understood(E), not depends_on_right(J, E).

% A right-hand column is covered when equated to an expression that does not
% depend on the right-hand side of the same join -- a column from elsewhere in
% the query, or a constant.
covers(J, I, C) :-
    join_right_instance(J, I),
    equality_side(J, Side, Other),
    expression_column(Side, I, C),
    independent(J, Other).

% A Unique Set of the join's right-hand Relation Instance.
right_unique_set(J, K) :-
    join_right_instance(J, I),
    instance_of(I, R),
    unique_set(K, R).

uncovered_member(J, K, C) :-
    right_unique_set(J, K),
    join_right_instance(J, I),
    instance_of(I, R),
    unique_set(K, R),
    unique_set_member(K, _, C),
    not covers(J, I, C).

has_uncovered_member(J, K) :- uncovered_member(J, K, _).
covered_unique_set(J, K) :- right_unique_set(J, K), not has_uncovered_member(J, K).

unique_join(J, K) :-
    covered_unique_set(J, K),
    join_kind(J, Kind),
    supported_join_kind(Kind).

proved(A) :- assertion(A, J), unique_join(J, _).
proof_key(A, K) :- assertion(A, J), unique_join(J, K).

% A Candidate Key is a Unique Set whose every member is known non-null. This
% is only ever additional evidence about a proof already made above: it never
% affects whether a Unique Set counts as covered, since a nullable Unique Set
% already suffices for ordinary equality-join uniqueness. See the Null
% Semantics section of docs/mvp-scope.md.
not_candidate_key(K) :- unique_set(K, R), unique_set_member(K, _, C), not column_not_null(R, C).
candidate_key(K) :- unique_set(K, _), not not_candidate_key(K).

proved_by_candidate_key(A) :- proof_key(A, K), candidate_key(K).

% Best-effort evidence for UNKNOWN: among the right-hand relation's Unique
% Sets, the one(s) missing the fewest members, and which members those are.
% A proved assertion has a fully covered Unique Set, so this never disagrees
% with a proof; it only ever fires for the Unique Sets that fell short.
uncovered_count(J, K, N) :- right_unique_set(J, K), N = #count { C : uncovered_member(J, K, C) }.
closest_uncovered_count(J, N) :- uncovered_count(J, _, _), N = #min { M : uncovered_count(J, _, M) }.
closest_unique_set(J, K) :- uncovered_count(J, K, N), closest_uncovered_count(J, N).

assertion_missing_member(A, K, C) :-
    assertion(A, J),
    closest_unique_set(J, K),
    uncovered_member(J, K, C).

% There is no `unknown` rule: UNKNOWN is precisely the absence of a proof, and
% the reporter reads it that way.
