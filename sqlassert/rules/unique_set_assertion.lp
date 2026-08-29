% Unique Set Assertions over relation expressions and output columns.

% Connects a Unique Set assertion to the relation expression it constrains.
#defined unique_set_assertion/2.
% Lists the ordered output columns named by a Unique Set assertion.
#defined unique_set_assertion_member/3.
% Marks a Unique Set assertion as requiring a Non-Null Unique Set.
#defined unique_set_assertion_key/1.

% Identifies an output column explicitly named by an assertion.
asserted_member(Assertion, Column) :- unique_set_assertion_member(Assertion, _, Column).

% Identifies a Unique Set column omitted from an assertion.
uncovered_assertion_member(Assertion, UniqueSet, Column) :-
    unique_set_assertion(Assertion, Relation),
    unique_set(UniqueSet, Relation),
    unique_set_column(UniqueSet, _, Column),
    not asserted_member(Assertion, Column).

% Records whether an assertion omits at least one column from a Unique Set.
has_uncovered_assertion_member(Assertion, UniqueSet) :-
    uncovered_assertion_member(Assertion, UniqueSet, _).

% Selects a Unique Set whose every column is named by an assertion.
covering_unique_set(Assertion, UniqueSet) :-
    unique_set_assertion(Assertion, Relation),
    unique_set(UniqueSet, Relation),
    not has_uncovered_assertion_member(Assertion, UniqueSet).

% Identifies an asserted column that is not independently known to be non-null.
lacks_independent_non_null(Assertion) :-
    unique_set_assertion(Assertion, Relation),
    unique_set_assertion_member(Assertion, _, Column),
    not non_null_column(Relation, Column).

% Determines whether an assertion's Non-Null Unique Set requirement is met.
key_requirement_met(Assertion) :-
    unique_set_assertion(Assertion, _),
    not unique_set_assertion_key(Assertion).
key_requirement_met(Assertion) :-
    unique_set_assertion_key(Assertion),
    not lacks_independent_non_null(Assertion).

% Marks an assertion as proved by a covering Unique Set that meets its requirement.
proved(Assertion) :- covering_unique_set(Assertion, _), key_requirement_met(Assertion).
% Records the Unique Set that proves an assertion.
proof_key(Assertion, UniqueSet) :- covering_unique_set(Assertion, UniqueSet), key_requirement_met(Assertion).

% Publishes a proved assertion as a new Unique Set on its constrained relation.
unique_set(uset(Assertion), Relation) :-
    unique_set_assertion(Assertion, Relation),
    proved(Assertion).
% Publishes each asserted column as a member of the asserted Unique Set.
unique_set_column(uset(Assertion), Position, Column) :-
    unique_set_assertion_member(Assertion, Position, Column),
    unique_set_assertion(Assertion, Relation),
    proved(Assertion).

% Counts the columns omitted from a known Unique Set by an assertion.
uncovered_count(Assertion, UniqueSet, Count) :-
    unique_set_assertion(Assertion, Relation),
    unique_set(UniqueSet, Relation),
    Count = #count { Column : uncovered_assertion_member(Assertion, UniqueSet, Column) }.
% Finds the smallest omitted-column count among the assertion's candidate Unique Sets.
closest_uncovered_count(Assertion, Count) :-
    uncovered_count(Assertion, _, _),
    Count = #min { OtherCount : uncovered_count(Assertion, _, OtherCount) }.
% Selects a candidate Unique Set with the fewest columns omitted by an assertion.
closest_unique_set(Assertion, UniqueSet) :-
    uncovered_count(Assertion, UniqueSet, Count),
    closest_uncovered_count(Assertion, Count).

% Reports an omitted column from the closest candidate Unique Set for diagnostics.
assertion_missing_member(Assertion, UniqueSet, Column) :-
    closest_unique_set(Assertion, UniqueSet),
    uncovered_assertion_member(Assertion, UniqueSet, Column).
