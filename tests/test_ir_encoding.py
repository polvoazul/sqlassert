import os
from pathlib import Path
import subprocess
import sys

from sqlassert import ir
from sqlassert.engine import Engine
from sqlassert.facts import encode
from sqlassert.ir.convert import IrParser
from sqlassert.knowledge import NonNullColumn, UniqueSet
from sqlassert.sql_parse import SqlParser


SQL = """
CREATE TABLE users(id INTEGER PRIMARY KEY);
SELECT *
FROM users AS buyer
JOIN users AS seller ON buyer.id = seller.id
"""

PROJECT = Path(__file__).parents[1]

RECURSIVE_ENCODING = """
from sqlassert.facts import encode
from sqlassert.ir.convert import IrParser
from sqlassert.sql_parse import SqlParser

sql = "CREATE VIEW loop AS SELECT * FROM loop; SELECT a, b, c FROM loop"
conversion = IrParser("duckdb").parse(SqlParser("duckdb").parse(sql))
print(encode(conversion.program, conversion.knowledge).facts)
"""


def _convert(sql: str = SQL):
    ast = SqlParser("duckdb").parse(sql)
    return IrParser("duckdb").parse(ast)


def test_encoding_is_deterministic_and_preserves_graph_identity():
    first = _convert()
    second = _convert()

    first_encoding = encode(first.program, first.knowledge)
    second_encoding = encode(second.program, second.knowledge)

    assert first_encoding.facts == second_encoding.facts
    assert set(first_encoding.node_to_symbol.values()) == set(first_encoding.symbol_to_node)
    assert "ir__join(" in first_encoding.facts
    assert "ir__join__left(" in first_encoding.facts
    assert "ir__relation_expr__output_columns(" in first_encoding.facts
    assert "__origin(" not in first_encoding.facts
    assert "relation_expression(" not in first_encoding.facts
    assert "property_preserving_input(" not in first_encoding.facts
    assert "ir__relation_expr(Node) :- ir__join(Node)." in first_encoding.inheritance_rules
    assert "ir__node(Node) :- ir__relation_expr(Node)." in first_encoding.inheritance_rules

    join = first.program.root
    assert isinstance(join, ir.Join)
    assert isinstance(join.left, ir.Alias)
    assert isinstance(join.right, ir.Alias)
    assert join.left is not join.right
    assert join.left.source is join.right.source is first.program.declarations[0]
    assert first_encoding.node_to_symbol[join.left] != first_encoding.node_to_symbol[join.right]
    assert first_encoding.node_to_symbol[join.left.source] == first_encoding.node_to_symbol[join.right.source]


def test_encoding_reflects_boolean_fields_and_public_knowledge():
    conversion = _convert("CREATE TABLE users(id INTEGER PRIMARY KEY); SELECT id FROM users")

    facts = encode(conversion.program, conversion.knowledge).facts

    assert "ir__relation_expr__is_schema_complete(" in facts
    assert "pub__non_null_column(" in facts
    assert "pub__unique_set(" in facts


def test_sql_lowering_constructs_knowledge_linked_to_ir_nodes():
    conversion = _convert("CREATE TABLE users(id INTEGER PRIMARY KEY); SELECT id FROM users")
    users = conversion.program.declarations[0]
    id_column = users.output_columns[0]
    unique_set = next(item for item in conversion.knowledge if isinstance(item, UniqueSet))
    non_null = next(item for item in conversion.knowledge if isinstance(item, NonNullColumn))

    assert unique_set.columns == frozenset({id_column})
    assert non_null.column is id_column


def test_encoding_uses_the_linked_knowledge_types_as_public_facts():
    conversion = _convert("CREATE TABLE users(id INTEGER); SELECT id FROM users")
    users = conversion.program.declarations[0]
    id_column = users.output_columns[0]
    knowledge = (NonNullColumn(column=id_column), UniqueSet(columns=frozenset({id_column})))

    encoding = encode(conversion.program, knowledge)
    facts = encoding.facts
    id_symbol = encoding.node_to_symbol[id_column]
    unique_set_symbol = next(
        line.removeprefix("pub__unique_set(").removesuffix(").")
        for line in facts.splitlines()
        if line.startswith("pub__unique_set(")
    )

    assert "pub__non_null_column(" in facts
    assert "pub__unique_set(" in facts
    assert f"pub__unique_set__columns({unique_set_symbol}, {id_symbol})." in facts


def test_engine_grounds_generated_inheritance_rules(monkeypatch):
    conversion = _convert()
    encoding = encode(conversion.program, conversion.knowledge)
    models: list[set[str]] = []
    monkeypatch.setattr("sqlassert.engine.rules", lambda: "#show relation/1. relation(Node) :- ir__relation_expr(Node).")

    Engine().run(encoding, lambda model: models.append({str(symbol) for symbol in model.symbols(shown=True)}))

    expected = {
        f"relation({symbol})"
        for node, symbol in encoding.node_to_symbol.items()
        if isinstance(node, ir.RelationExpr)
    }
    assert models == [expected]


def test_view_and_cte_occurrences_share_their_linked_named_relation():
    programs = (
        """CREATE TABLE users(id INTEGER PRIMARY KEY); CREATE VIEW active AS SELECT id FROM users;
        SELECT * FROM active AS first JOIN active AS second ON first.id = second.id""",
        """CREATE TABLE users(id INTEGER PRIMARY KEY); WITH active AS (SELECT id FROM users)
        SELECT * FROM active AS first JOIN active AS second ON first.id = second.id""",
    )

    for sql in programs:
        conversion = _convert(sql)
        join = conversion.program.root
        assert isinstance(join, ir.Join)
        assert isinstance(join.left, ir.Alias)
        assert isinstance(join.right, ir.Alias)
        assert join.left is not join.right
        assert join.left.source is join.right.source


def test_recursive_definitions_encode_without_a_python_cycle():
    conversion = _convert("CREATE VIEW loop AS SELECT id FROM loop; SELECT * FROM loop")
    recursive = [
        node
        for node in encode(conversion.program, conversion.knowledge).symbol_to_node.values()
        if isinstance(node, ir.RecursiveRelation)
    ]

    assert len(recursive) == 1
    encoding = encode(conversion.program, conversion.knowledge)
    assert recursive[0] in encoding.node_to_symbol


def test_recursive_encoding_is_stable_across_python_hash_seeds():
    encodings = {
        subprocess.run(
            [sys.executable, "-c", RECURSIVE_ENCODING],
            cwd=PROJECT,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            capture_output=True,
            check=True,
            text=True,
        ).stdout
        for seed in (1, 2, 4)
    }

    assert len(encodings) == 1


def test_encoding_states_aggregate_structure_without_precomputing_its_unique_set():
    conversion = _convert("SELECT customer_id FROM orders GROUP BY customer_id")

    facts = encode(conversion.program, conversion.knowledge).facts

    assert "ir__aggregate__grouping_outputs(" in facts
    assert "pub__unique_set(" not in facts


def test_grouped_bare_columns_are_encoded_as_any_aggregates():
    conversion = _convert("SELECT derf, user_id, SUM(amount) AS spent FROM orders GROUP BY 1")
    aggregate = conversion.program.root

    assert isinstance(aggregate, ir.Aggregate)
    assert isinstance(aggregate.output_columns[0].expression, ir.ColumnRef)
    assert isinstance(aggregate.output_columns[1].expression, ir.AnyAggregate)
    assert isinstance(aggregate.output_columns[1].expression.input, ir.ColumnRef)
    assert isinstance(aggregate.output_columns[2].expression, ir.OpaqueExpression)
    assert "ir__any_aggregate__input(" in encode(conversion.program, conversion.knowledge).facts
