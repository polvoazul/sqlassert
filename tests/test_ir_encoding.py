import os
from pathlib import Path
import subprocess
import sys

from sqlassert import ir
from sqlassert.facts import encode
from sqlassert.ir.convert import IrParser
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
    assert "instance_of" not in first_encoding.facts
    assert "join_output_definition" not in first_encoding.facts

    join = first.program.root
    assert isinstance(join, ir.Join)
    assert isinstance(join.left, ir.Alias)
    assert isinstance(join.right, ir.Alias)
    assert join.left is not join.right
    assert join.left.source is join.right.source is first.program.declarations[0]
    assert first_encoding.node_to_symbol[join.left] != first_encoding.node_to_symbol[join.right]
    assert first_encoding.node_to_symbol[join.left.source] == first_encoding.node_to_symbol[join.right.source]


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

    assert "aggregate_grouping_output(" in facts
    assert "unique_set(" not in facts


def test_grouped_bare_columns_are_encoded_as_any_aggregates():
    conversion = _convert("SELECT derf, user_id, SUM(amount) AS spent FROM orders GROUP BY 1")
    aggregate = conversion.program.root

    assert isinstance(aggregate, ir.Aggregate)
    assert isinstance(aggregate.output_columns[0].expression, ir.ColumnRef)
    assert isinstance(aggregate.output_columns[1].expression, ir.AnyAggregate)
    assert isinstance(aggregate.output_columns[1].expression.input, ir.ColumnRef)
    assert isinstance(aggregate.output_columns[2].expression, ir.OpaqueExpression)
    assert "arbitrary_group_value(" in encode(conversion.program, conversion.knowledge).facts
