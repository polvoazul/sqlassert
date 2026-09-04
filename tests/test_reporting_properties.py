"""Assertion outcomes are direct public-property lookups."""

import clingo
import pytest

from sqlassert import Outcome, ir
from sqlassert.engine import Engine
from sqlassert.facts import encode
from sqlassert.ir.convert import IrParser
from sqlassert.properties import UniqueJoin
from sqlassert.reporting import Reporter
from sqlassert.sql_parse import SqlParser


def _convert(sql):
    return IrParser("duckdb").parse(SqlParser("duckdb").parse(sql))


def _report_facts(encoding, assertions, facts):
    reporter = Reporter(encoding)
    control = clingo.Control()
    control.add("base", [], facts)
    control.ground([("base", [])])
    control.solve(on_model=reporter.on_model)
    return reporter.report(assertions)


@pytest.mark.parametrize(
    ("marker", "candidate_fact", "expected"),
    [("UNIQUE", False, Outcome.PROVED), ("PRIMARY KEY", False, Outcome.UNKNOWN),
     ("PRIMARY KEY", True, Outcome.PROVED)],
)
def test_requested_public_property_determines_the_outcome(marker, candidate_fact, expected):
    conversion = _convert(f"SELECT id FROM users /**{marker}(id)**/")
    encoding = encode(conversion.program, ())
    assertion = conversion.program.assertions[0]
    requested = f"asserted_columns({encoding.node_to_symbol[assertion]})"
    column = encoding.node_to_symbol[next(iter(assertion.property.columns))]
    facts = f"pub__unique_set({requested}). pub__unique_set__columns({requested}, {column})."
    if candidate_fact:
        facts += f"pub__candidate_key({requested})."

    report = _report_facts(encoding, (assertion,), facts)

    assert report.assertions[0].outcome is expected


def test_internal_coverage_does_not_replace_the_requested_public_property():
    conversion = _convert("SELECT id, name FROM users /**UNIQUE(id, name)**/")
    encoding = encode(conversion.program, ())
    assertion = conversion.program.assertions[0]
    assertion_id = encoding.node_to_symbol[assertion]
    column = encoding.node_to_symbol[next(c for c in assertion.property.columns if c.name == "id")]

    report = _report_facts(encoding, (assertion,), f"""
        pub__unique_set(known).
        pub__unique_set__columns(known, {column}).
        covers_unique_set(asserted_columns({assertion_id}), known).
    """)

    assert report.assertions[0].outcome is Outcome.UNKNOWN


@pytest.mark.parametrize("same_join", [True, False])
def test_public_unique_join_matches_the_requested_join(same_join):
    conversion = _convert("SELECT * FROM users /**UNIQUE**/ JOIN orders ON users.id = orders.user_id")
    encoding = encode(conversion.program, ())
    assertion = conversion.program.assertions[0]
    join = encoding.node_to_symbol[assertion.property.join] if same_join else "another_join"

    report = _report_facts(encoding, (assertion,), f"""
        pub__unique_join(known).
        pub__unique_join__join(known, {join}).
    """)

    assert report.assertions[0].outcome is (Outcome.PROVED if same_join else Outcome.UNKNOWN)


def test_accepted_unique_join_is_reported_and_preserves_left_uniqueness():
    conversion = _convert("""
        CREATE TABLE users(id INTEGER PRIMARY KEY);
        CREATE TABLE orders(user_id INTEGER);
        SELECT users.id FROM users
        /**UNIQUE**/ JOIN orders ON users.id = orders.user_id
        /**UNIQUE(id)**/
    """)
    join = next(a.property.join for a in conversion.program.assertions if isinstance(a.property, UniqueJoin))
    program = ir.Program(
        named_relations=conversion.program.named_relations,
        root=conversion.program.root,
        assertions=conversion.program.assertions,
        declarations=(UniqueJoin(join=join),),
    )
    encoding = encode(program, conversion.knowledge)
    reporter = Reporter(encoding)
    atoms = set()

    def capture(model):
        reporter.on_model(model)
        atoms.update(str(atom) for atom in model.symbols(atoms=True))

    Engine().run(encoding, capture)
    report = reporter.report(program.assertions)

    assert [a.outcome for a in report.assertions] == [Outcome.PROVED, Outcome.PROVED]
    assert not any(atom.startswith("pub__proved(") for atom in atoms)
