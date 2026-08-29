from dataclasses import FrozenInstanceError, is_dataclass

import pytest

from sqlassert import ir
from sqlassert.knowledge import Knowledge
from sqlassert.provenance import Origin, SQL


def _origin(detail: str) -> Origin:
    return Origin(SQL, detail)


def test_ir_nodes_are_frozen_identity_dataclasses_with_direct_references():
    table_column = ir.OutputColumn(
        origin=_origin("users.id"),
        name="id",
        expression=ir.OpaqueExpression(origin=_origin("users.id"), description="table column"),
    )
    users = ir.NamedRelation(
        origin=_origin("CREATE TABLE users"),
        output_columns=(table_column,),
        name="users",
        role=ir.RelationRole.TABLE,
        is_schema_complete=True,
    )
    alias_column = ir.OutputColumn(
        origin=_origin("users AS u"),
        name="id",
        expression=ir.ColumnRef(origin=_origin("u.id"), column=table_column),
    )
    first = ir.Alias(origin=_origin("users AS u"), output_columns=(alias_column,), source=users, name="u")
    second = ir.Alias(origin=_origin("users AS u"), output_columns=(alias_column,), source=users, name="u")

    assert first is not second
    assert first != second
    assert first.source is second.source is users
    assert isinstance(first.output_columns[0].expression, ir.ColumnRef)
    assert first.output_columns[0].expression.column is table_column
    assert first.origin == _origin("users AS u")
    with pytest.raises(FrozenInstanceError):
        first.name = "changed"  # ty: ignore[invalid-assignment]


def test_assertions_point_directly_into_the_relation_graph():
    empty = ir.OpaqueRelation(origin=_origin("unsupported"), output_columns=(), description="unsupported")
    join = ir.Join(origin=_origin("JOIN"), output_columns=(), kind=ir.INNER, left=empty, right=empty)
    join_assertion = ir.UniqueJoinAssertion(origin=_origin("marker"), subject=join)
    set_assertion = ir.UniqueSetAssertion(
        origin=_origin("marker"), subject=empty, columns=(), is_candidate_key=False
    )
    program = ir.Program(declarations=(), root=join, assertions=(join_assertion, set_assertion))

    assert program.root is join
    assert join_assertion.subject is join
    assert set_assertion.subject is empty


def test_only_concrete_ir_nodes_can_be_constructed():
    for node_type in (ir.Node, ir.ScalarExpr, ir.RelationExpr, ir.Assertion):
        with pytest.raises(TypeError, match="abstract"):
            node_type(origin=_origin("abstract"))  # ty: ignore[missing-argument]


def test_knowledge_is_an_abstract_ir_like_type():
    with pytest.raises(TypeError, match="abstract"):
        Knowledge()


def test_every_node_subclass_is_a_dataclass_without_repeating_the_decorator():
    pending = list(ir.Node.__subclasses__())
    found: list[type[ir.Node]] = []
    while pending:
        node_type = pending.pop()
        found.append(node_type)
        pending.extend(node_type.__subclasses__())

    assert found
    assert all(is_dataclass(node_type) for node_type in found)
    assert all(
        set(node_type.__annotations__) <= set(node_type.__dataclass_fields__)
        for node_type in found
    )
