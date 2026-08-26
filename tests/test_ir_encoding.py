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
        for declaration in conversion.program.declarations
        for node in ir.relation_nodes(declaration)
        if isinstance(node, ir.RecursiveRelation)
    ]

    assert len(recursive) == 1
    encoding = encode(conversion.program, conversion.knowledge)
    assert recursive[0] in encoding.node_to_symbol
