"""Columns a reference adds to a table: an undeclared table gains each column
the program reads from it, through views, CTEs and subqueries too; a declared
table keeps exactly its CREATE's columns, and a qualified star (`a.*`) is no
column."""

from __future__ import annotations

import pytest

from sqlassert import ir
from sqlassert.ir.convert import IrParser
from sqlassert.sql_parse import SqlParser


def table_columns(sql: str, name: str) -> list[str]:
    program = IrParser("duckdb", True).parse(SqlParser("duckdb").parse(sql)).program
    (table,) = [
        node for node in program.nodes()
        if isinstance(node, ir.NamedRelation) and node.role is ir.RelationRole.TABLE and node.name == name
    ]
    return sorted(column.name for column in table.output_columns)


@pytest.mark.parametrize(("sql", "columns"), [
    ("select a.k from u a;", ["k"]),
    ("select x.k from (select * from u) as x;", ["k"]),
    ("with c as (select * from u) select c.k from c;", ["k"]),
    ("create view v as select * from u; select v.k, v.j from v;", ["j", "k"]),
])
def test_an_undeclared_table_gains_the_columns_read_from_it(sql, columns):
    assert table_columns(sql, "u") == columns


@pytest.mark.parametrize("query", [
    "select a.z from t a;",
    "select a.* from t a;",
    "select t.* from t;",
    "select a.* from t a union all select 1, 2;",
])
def test_a_declared_table_keeps_its_columns(query):
    assert table_columns("create table t (x integer, y integer); " + query, "t") == ["x", "y"]
