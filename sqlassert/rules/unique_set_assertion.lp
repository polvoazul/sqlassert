% Unique Set Assertions.
%
% `/**UNIQUE(...)**/` and `/**PRIMARY KEY(...)**/` assert that a named set of
% a Select Expression's own output columns forms a Unique Set -- the same
% concept `unique_set/2` already models throughout this engine, whether
% derived from Knowledge or from structural propagation. A Unique Set
% Assertion adds nothing structurally new to that concept: it is proved when
% some already-known Unique Set of the same Relation Definition is fully
% covered by the asserted columns -- a smaller proven set makes any superset
% of it provable too, the same reasoning `RelationFacts.is_unique` already
% applies for callers -- performed here inside the same solve so a proof also
% feeds forward, through the existing propagation rules, exactly like a
% Knowledge-declared Unique Set would.
%
% `PRIMARY KEY` additionally requires every asserted column to already be
% independently known non-null -- from Knowledge, DDL, or structural
% propagation, never from this same rule set's own conclusions: an assertion
% never certifies its own precondition, so `column_not_null` is not fed
% forward from a proved assertion the way `unique_set` is.
%
% Deterministic and stratified by policy: facts, normal derivation, stratified
% default negation, and one level of positive recursion through `unique_set`
% that mirrors the recursion `rules/propagation.lp` already has for
% Filter/Project. See docs/clingo-rule-policy.md.
%
% Variable naming used in this file: AssertionId = assertion id,
% DefinitionId = Relation Definition id, UniqueSetKey = an existing Unique Set
% key, ColumnName = column name, Count/OtherCount = counts.

#defined unique_set_assertion/2.
#defined unique_set_assertion_member/3.
#defined unique_set_assertion_key/1.

asserted_member(AssertionId, ColumnName) :- unique_set_assertion_member(AssertionId, _, ColumnName).

uncovered_assertion_member(AssertionId, UniqueSetKey, ColumnName) :-
    unique_set_assertion(AssertionId, DefinitionId),
    unique_set(UniqueSetKey, DefinitionId),
    unique_set_member(UniqueSetKey, _, ColumnName),
    not asserted_member(AssertionId, ColumnName).

has_uncovered_assertion_member(AssertionId, UniqueSetKey) :-
    uncovered_assertion_member(AssertionId, UniqueSetKey, _).

covering_unique_set(AssertionId, UniqueSetKey) :-
    unique_set_assertion(AssertionId, DefinitionId),
    unique_set(UniqueSetKey, DefinitionId),
    not has_uncovered_assertion_member(AssertionId, UniqueSetKey).

lacks_independent_non_null(AssertionId) :-
    unique_set_assertion(AssertionId, DefinitionId),
    unique_set_assertion_member(AssertionId, _, ColumnName),
    not column_not_null(DefinitionId, ColumnName).

key_requirement_met(AssertionId) :-
    unique_set_assertion(AssertionId, _),
    not unique_set_assertion_key(AssertionId).
key_requirement_met(AssertionId) :-
    unique_set_assertion_key(AssertionId),
    not lacks_independent_non_null(AssertionId).

proved(AssertionId) :- covering_unique_set(AssertionId, _), key_requirement_met(AssertionId).
proof_key(AssertionId, UniqueSetKey) :- covering_unique_set(AssertionId, UniqueSetKey), key_requirement_met(AssertionId).

% A proved assertion's Unique Set becomes exactly the same kind of fact a
% Knowledge declaration or structural propagation would have produced, so
% `rules/propagation.lp` and every other consumer of `unique_set/2` treat it
% identically -- including a later statement's own reference to the same
% Relation Definition, with no separate cross-statement logic needed.
% `uset(AssertionId)` is a fresh compound identifier, safe to invent since
% AssertionId is already ground; it never collides with a structurally
% derived key, so an assertion can never cover itself by pure self-support.
unique_set(uset(AssertionId), DefinitionId) :-
    unique_set_assertion(AssertionId, DefinitionId), proved(AssertionId).
unique_set_member(uset(AssertionId), Position, ColumnName) :-
    unique_set_assertion_member(AssertionId, Position, ColumnName),
    unique_set_assertion(AssertionId, DefinitionId),
    proved(AssertionId).

% Best-effort evidence for UNKNOWN: among the Relation Definition's Unique
% Sets, the one(s) the asserted columns come closest to covering, and which
% members are missing from the assertion.
uncovered_count(AssertionId, UniqueSetKey, Count) :-
    unique_set_assertion(AssertionId, DefinitionId),
    unique_set(UniqueSetKey, DefinitionId),
    Count = #count { ColumnName : uncovered_assertion_member(AssertionId, UniqueSetKey, ColumnName) }.
closest_uncovered_count(AssertionId, Count) :-
    uncovered_count(AssertionId, _, _),
    Count = #min { OtherCount : uncovered_count(AssertionId, _, OtherCount) }.
closest_unique_set(AssertionId, UniqueSetKey) :-
    uncovered_count(AssertionId, UniqueSetKey, Count),
    closest_uncovered_count(AssertionId, Count).

assertion_missing_member(AssertionId, UniqueSetKey, ColumnName) :-
    closest_unique_set(AssertionId, UniqueSetKey),
    uncovered_assertion_member(AssertionId, UniqueSetKey, ColumnName).

% There is no `unknown` rule: UNKNOWN is precisely the absence of a proof, and
% the reporter reads it that way.
