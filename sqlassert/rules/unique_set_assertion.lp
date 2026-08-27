% Unique Set Assertions over relation expressions and output columns.

#defined unique_set_assertion/2.
#defined unique_set_assertion_member/3.
#defined unique_set_assertion_key/1.

asserted_member(Assertion, Column) :- unique_set_assertion_member(Assertion, _, Column).

uncovered_assertion_member(Assertion, UniqueSet, Column) :-
    unique_set_assertion(Assertion, Relation),
    unique_set(UniqueSet, Relation),
    unique_set_column(UniqueSet, _, Column),
    not asserted_member(Assertion, Column).

has_uncovered_assertion_member(Assertion, UniqueSet) :-
    uncovered_assertion_member(Assertion, UniqueSet, _).

covering_unique_set(Assertion, UniqueSet) :-
    unique_set_assertion(Assertion, Relation),
    unique_set(UniqueSet, Relation),
    not has_uncovered_assertion_member(Assertion, UniqueSet).

lacks_independent_non_null(Assertion) :-
    unique_set_assertion(Assertion, Relation),
    unique_set_assertion_member(Assertion, _, Column),
    not non_null_column(Relation, Column).

key_requirement_met(Assertion) :-
    unique_set_assertion(Assertion, _),
    not unique_set_assertion_key(Assertion).
key_requirement_met(Assertion) :-
    unique_set_assertion_key(Assertion),
    not lacks_independent_non_null(Assertion).

proved(Assertion) :- covering_unique_set(Assertion, _), key_requirement_met(Assertion).
proof_key(Assertion, UniqueSet) :- covering_unique_set(Assertion, UniqueSet), key_requirement_met(Assertion).

unique_set(uset(Assertion), Relation) :-
    unique_set_assertion(Assertion, Relation),
    proved(Assertion).
unique_set_column(uset(Assertion), Position, Column) :-
    unique_set_assertion_member(Assertion, Position, Column),
    unique_set_assertion(Assertion, Relation),
    proved(Assertion).

uncovered_count(Assertion, UniqueSet, Count) :-
    unique_set_assertion(Assertion, Relation),
    unique_set(UniqueSet, Relation),
    Count = #count { Column : uncovered_assertion_member(Assertion, UniqueSet, Column) }.
closest_uncovered_count(Assertion, Count) :-
    uncovered_count(Assertion, _, _),
    Count = #min { OtherCount : uncovered_count(Assertion, _, OtherCount) }.
closest_unique_set(Assertion, UniqueSet) :-
    uncovered_count(Assertion, UniqueSet, Count),
    closest_uncovered_count(Assertion, Count).

assertion_missing_member(Assertion, UniqueSet, Column) :-
    closest_unique_set(Assertion, UniqueSet),
    uncovered_assertion_member(Assertion, UniqueSet, Column).
