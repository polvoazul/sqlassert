% Unique Join Assertions.
%
% A Unique Join is proved when every member of some Unique Set of the join's
% right-hand Relation Instance is determined by the join predicate. Absence of
% such a proof is UNKNOWN, never a Disproof.
%
% Deterministic and stratified by policy: facts, normal derivation, and default
% negation only. See docs/clingo-rule-policy.md.

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
#defined expression_opaque/1.
#defined assertion/2.

% An equality constrains both of its sides.
equality_side(J, Side, Other) :- join_equality(J, Side, Other).
equality_side(J, Other, Side) :- join_equality(J, Side, Other).

% Only expressions conversion actually modelled can determine a value.
% Opaque expressions deliberately determine nothing.
understood(E) :- expression_column(E, _, _).

depends_on_right(J, E) :- expression_column(E, I, _), join_right_instance(J, I).
independent(J, E) :- equality_side(J, E, _), understood(E), not depends_on_right(J, E).

% A right-hand column is covered when equated to an expression that does not
% depend on the right-hand side of the same join.
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

uncovered_member(J, K) :-
    right_unique_set(J, K),
    join_right_instance(J, I),
    instance_of(I, R),
    unique_set(K, R),
    unique_set_member(K, _, C),
    not covers(J, I, C).

covered_unique_set(J, K) :- right_unique_set(J, K), not uncovered_member(J, K).

% This slice proves INNER joins only; every other kind stays UNKNOWN.
% LEFT joins arrive with a later slice.
unique_join(J, K) :- covered_unique_set(J, K), join_kind(J, inner).

proved(A) :- assertion(A, J), unique_join(J, _).
proof_key(A, K) :- assertion(A, J), unique_join(J, K).

% There is no `unknown` rule: UNKNOWN is precisely the absence of a proof, and
% the reporter reads it that way.
