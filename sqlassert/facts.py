"""Turn the bound IR and Knowledge into ground ASP facts.

This module is where the two separately represented inputs — query structure and
what is known about relations — finally meet. It states facts only; every
inference lives in the rule files.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlassert import ir, naming
from sqlassert.knowledge import Knowledge
from sqlassert.naming import ConstantNames


@dataclass(frozen=True)
class GroundFacts:
    text: str
    key_columns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def ground_facts(program: ir.BoundProgram, knowledge: Knowledge, names: ConstantNames) -> GroundFacts:
    lines: list[str] = []
    key_columns: dict[str, tuple[str, ...]] = {}

    for definition in program.definitions:
        lines.append(f"relation({definition.id}).")
        known = knowledge.relation(definition.name) if definition.name else None
        if known is None:
            continue
        for unique_set in known.unique_sets:
            key = names.new(naming.KEY, "_".join(unique_set.columns))
            key_columns[key] = unique_set.columns
            lines.append(f"unique_set({key}, {definition.id}).")
            lines.extend(f"unique_set_member({key}, {_text(column)})." for column in unique_set.columns)

    for instance in ir.instances(program.root):
        lines.append(f"instance_of({instance.id}, {instance.definition_id}).")

    for join in ir.joins(program.root):
        lines.append(f"join_kind({join.id}, {join.kind}).")
        lines.extend(f"join_left_instance({join.id}, {instance.id})." for instance in ir.instances(join.left))
        lines.extend(f"join_right_instance({join.id}, {instance.id})." for instance in ir.instances(join.right))
        for equality in join.equalities:
            lines.extend(_expression_facts(equality.left))
            lines.extend(_expression_facts(equality.right))
            lines.append(f"join_equality({join.id}, {equality.left.id}, {equality.right.id}).")

    lines.extend(f"assertion({assertion.id}, {assertion.join_id})." for assertion in program.assertions)

    return GroundFacts("\n".join(lines), key_columns)


def _expression_facts(expression: ir.Expression) -> list[str]:
    if isinstance(expression, ir.ColumnReference):
        return [f"expression_column({expression.id}, {expression.instance_id}, {_text(expression.column)})."]
    return [f"expression_opaque({expression.id})."]


def _text(value: str) -> str:
    escaped = value.lower().replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
