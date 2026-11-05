"""The closed unions name every concrete IR node, so a `match` over one that
ends in `assert_never` covers every node a program can hold."""

from __future__ import annotations

from typing import get_args

import pytest

from sqlassert import ir


def _concrete_subclasses(cls: type) -> set[type]:
    found: set[type] = set()
    for subclass in cls.__subclasses__():
        found |= _concrete_subclasses(subclass)
        if not getattr(subclass, "__ir_abstract__", False):
            found.add(subclass)
    return found


@pytest.mark.parametrize(("base", "union"), [(ir.ScalarExpr, ir.AnyScalar), (ir.RelationExpr, ir.AnyRelation)])
def test_a_closed_union_names_every_concrete_subclass(base: type, union):
    assert _concrete_subclasses(base) == set(get_args(union.__value__))
