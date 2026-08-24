% Uniqueness propagated through Project.
%
% A Project's own Relation Definition earns a new Unique Set for every Unique
% Set of its input relation that survives the projection whole: every member
% column must appear, unrenamed-computation, among the projected outputs. The
% new Unique Set's members are the *output* names, so a later join against the
% projected alias sees the columns it actually has.
%
% Filter needs no rules here: it reuses its input's own Relation Definition
% (see ir/model.py), so the input's existing unique_set facts already apply.
%
% Deterministic and stratified by policy: facts and normal derivation only, one
% level of default negation over facts already derived from base facts alone.
% See docs/clingo-rule-policy.md.

#defined project_input/2.
#defined project_output/3.

% An output column whose source expression is exactly one member column of the
% Project's input relation, unrenamed-computation.
projected_column(D, C, Out) :-
    project_output(D, Out, E),
    project_input(D, I),
    expression_column(E, I, C).

% A Unique Set of the relation a Project draws from -- a candidate for
% propagation, pending a check that every member survived the projection.
candidate_unique_set(K, D) :- project_input(D, I), instance_of(I, R), unique_set(K, R).

uncovered_projected_member(K, D, C) :-
    candidate_unique_set(K, D),
    unique_set_member(K, _, C),
    not projected_column(D, C, _).

% `pk(K, D)` is a fresh compound identifier for the Unique Set Project D earns
% from source Unique Set K -- safe to invent since K and D are already ground.
propagated_key(pk(K, D), K, D) :-
    candidate_unique_set(K, D),
    not uncovered_projected_member(K, D, _).

unique_set(PK, D) :- propagated_key(PK, K, D).
unique_set_member(PK, Pos, Out) :-
    propagated_key(PK, K, D),
    unique_set_member(K, Pos, C),
    projected_column(D, C, Out).
