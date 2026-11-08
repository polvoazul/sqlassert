from dataclasses import replace

import pytest

from sqlassert import analyze, ir, python_engine
from sqlassert.ir.convert import IrParser
from sqlassert.properties import ManyToOneJoin, NonNullColumn, Property, UniqueSet
from sqlassert.python_engine import Engine, Result, Stratum, all_of, analysis, any_of, cache, current_arena, unknown
from sqlassert.rules.join_cardinality import _bound_columns, many_to_one_join
from sqlassert.rules.nullability import non_null_column
from sqlassert.rules.uniqueness import unique_set
from sqlassert.sql_parse import SqlParser


SQL = """
CREATE TABLE users(id INTEGER PRIMARY KEY);
CREATE TABLE events(user_id INTEGER);
SELECT events.user_id FROM events /**ASSERT MANY TO ONE**/ JOIN users ON events.user_id = users.id
"""


def convert(sql=SQL):
    return IrParser("duckdb").parse(SqlParser("duckdb").parse(sql))


def walk(proof):
    yield proof
    for premise in proof.premises:
        yield from walk(premise)


def test_proof_composition_retains_unknowns_and_alternative_justifications():
    first = Result(True, "first")
    second = Result(True, "second")
    missing = unknown("missing")

    assert (first & second).value
    assert not (first & missing).value
    alternatives = any_of((first, missing, second))
    assert alternatives.value
    assert alternatives.premises == [first, missing, second]
    assert all_of(()).value
    assert not any_of(()).value
    with pytest.raises(TypeError, match=".value"):
        bool(first)


def test_cache_normalizes_arguments_and_is_scoped_to_one_analysis():
    conversion = convert()
    join = conversion.program.assertions[0].property.join
    calls = []

    @cache
    def check(node, option=True):
        calls.append(node)
        return Result(True, "checked", (node,))

    with Engine().session(conversion.program):
        first = check(join)
        assert first is check(node=join, option=True)
        assert check(replace(join)) is not first
        with pytest.raises(AssertionError, match="arena is already active"):
            with Engine().session(conversion.program):
                pytest.fail("Nested analysis should not start")
        assert check(join) is first
    with Engine().session(conversion.program):
        assert check(join) is not first
    assert len(calls) == 3
    with pytest.raises(LookupError):
        current_arena()


def test_session_releases_arena_when_analysis_raises():
    with pytest.raises(ValueError, match="analysis failed"):
        with Engine().session(ir.Program()):
            raise ValueError("analysis failed")
    with pytest.raises(LookupError):
        current_arena()
    with Engine().session(ir.Program()) as arena:
        assert current_arena() is arena


def test_cycles_fail_explicitly_and_failed_evaluations_do_not_poison_cache():
    @cache
    def recursive():
        return recursive()

    with Engine().session(ir.Program()) as arena:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="cyclic property dependency"):
                recursive()
            assert not arena.evaluating
            assert not arena.cache


def test_an_analysis_may_not_use_a_higher_stratum_fact_about_the_same_relation(monkeypatch):
    monkeypatch.setattr(python_engine, "analysis_functions", python_engine.analysis_functions.copy())
    conversion = convert()
    join = conversion.program.assertions[0].property.join

    @analysis(Stratum.UNIQUE_SET)
    def _high(relation):
        return Result(True, "high", (relation,))

    @analysis(Stratum.NON_NULL)
    def _low(relation, target):
        return _high(target)

    with Engine().session(conversion.program):
        assert _low(join, join.right).value  # Higher stratum about an input relation.
        with pytest.raises(AssertionError, match="own or a lower stratum"):
            _low(join, join)
        assert _high(join).value  # The failed call left no stale frame behind.


def test_any_analysis_function_accepts_declarations_without_a_rule_branch(monkeypatch):
    monkeypatch.setattr(Property, "_subclasses", Property._subclasses.copy())
    monkeypatch.setattr(python_engine, "analysis_functions", python_engine.analysis_functions.copy())
    class ExampleProperty(Property):
        column: ir.OutputColumn

    calls = []

    @analysis(Stratum.NON_NULL)
    def example_property(column):
        calls.append(column)
        return unknown("No derivation", column)

    conversion = convert()
    column = conversion.program.named_relations[0].output_columns[0]
    declaration = ExampleProperty(column=column)
    program = replace(conversion.program, declarations=(declaration,))
    with Engine().session(program) as arena:
        result = example_property(column)
        assert result.value
        assert arena.prove(ExampleProperty(column=column)) is result
        assert any(declaration in p.subjects for p in walk(result) if p.kind == "accepted")
        assert not calls
    with Engine().session(conversion.program):
        assert not example_property(column).value
    assert calls == [column]


def test_declared_unique_join_preserves_left_keys_without_an_inferred_proof():
    conversion = convert("""
        CREATE TABLE users(id INTEGER PRIMARY KEY);
        CREATE TABLE events(user_id INTEGER);
        SELECT users.id FROM users /**ASSERT MANY TO ONE**/ JOIN events ON users.id = events.user_id /**ASSERT UNIQUE(id)**/
    """)
    join = conversion.program.assertions[0].property.join
    program = replace(conversion.program, declarations=(ManyToOneJoin(join=join),))
    report = Engine().run(program, conversion.knowledge)
    assert report.proved
    assert all(a.evidence is not None and a.evidence.value for a in report.assertions)
    evidence = report.assertions[1].evidence
    assert evidence is not None
    assert "Declared ManyToOneJoin" in evidence.explain()


def test_join_proof_retains_schema_and_equality_sources_and_reuses_cached_claims():
    conversion = convert()
    join = conversion.program.assertions[0].property.join
    with Engine().session(conversion.program, conversion.knowledge) as arena:
        proof = many_to_one_join(join)
        assert proof is arena.prove(ManyToOneJoin(join=join))
        assert _bound_columns(join, "right") is _bound_columns(join, "right")
        columns = _bound_columns(join, "right")
        assert type(columns) is Result
        assert columns.value.relation is join.right
        assert columns.value.columns == set(join.right.output_columns)
        assert set(columns.members) == columns.value.columns
        nodes = tuple(walk(proof))
        assert any(node.kind == "accepted" for node in nodes)
        assert any(join.conjuncts[0] in node.subjects for node in nodes)
        text = proof.explain()
        assert "PROVED: many_to_one_join" in text
        assert "line" in text
        assert "Equality fixes the matched column" in text


def test_missing_composite_key_column_is_retained_as_unknown_evidence():
    conversion = convert("""
        CREATE TABLE users(id INTEGER, region INTEGER, PRIMARY KEY(id, region));
        CREATE TABLE events(user_id INTEGER);
        SELECT * FROM events /**ASSERT MANY TO ONE**/ JOIN users ON events.user_id = users.id
    """)
    proof = Engine().run(conversion.program, conversion.knowledge).assertions[0].evidence
    assert proof is not None
    assert not proof.value
    missing = [p for p in walk(proof) if p.reason == "Unique-set columns are not covered"]
    assert len(missing) == 1
    assert [column.name for column in missing[0].subjects] == ["region"]


def test_two_covering_keys_retain_both_justifications():
    conversion = convert("""
        CREATE TABLE users(id INTEGER UNIQUE, email VARCHAR COLLATE c UNIQUE);
        CREATE TABLE events(user_id INTEGER, email VARCHAR COLLATE c);
        SELECT * FROM events /**ASSERT MANY TO ONE**/ JOIN users
        ON events.user_id = users.id AND events.email = users.email
    """)
    proof = Engine().run(conversion.program, conversion.knowledge).assertions[0].evidence
    assert proof is not None
    assert proof.value
    accepted = {p for p in walk(proof) if p.kind == "accepted"}
    assert len(accepted) == 2


def test_unique_set_and_non_null_declarations_use_the_same_decorator():
    conversion = convert("CREATE TABLE events(user_id INTEGER); SELECT user_id FROM events")
    column = conversion.program.root.output_columns[0]
    columns = frozenset((column,))
    program = replace(conversion.program, declarations=(UniqueSet(columns=columns), NonNullColumn(column=column)))
    with Engine().session(program):
        assert unique_set(columns).value
        assert non_null_column(column).value
    with Engine().session(conversion.program):
        assert not unique_set(columns).value
        assert not non_null_column(column).value


def test_default_entry_point_returns_python_proof_evidence():
    report = analyze(SQL)
    assert report.proved
    evidence = report.assertions[0].evidence
    assert evidence is not None
    assert evidence.value
    assert "Accepted UniqueSet" in evidence.explain()

