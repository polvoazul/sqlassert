"""Turn the IR and Knowledge into ground ASP facts.

This module is where the two separately represented inputs — query structure and
what is known about relations — finally meet. It states facts only; every
inference lives in the rule files.
"""

from __future__ import annotations

from sqlassert import ir, naming
from sqlassert.knowledge import Knowledge
from sqlassert.naming import NameGiver


def ground_facts(program: ir.Program, knowledge: Knowledge, names: NameGiver) -> str:
    lines: list[str] = []

    for definition in program.definitions:
        lines.append(f"relation({definition.id}).")
        known = knowledge.relation(definition.name) if definition.name else None
        if known is None:
            continue
        lines.extend(
            f"column_not_null({definition.id}, {_text(column.name)})."
            for column in known.columns
            if not column.nullable
        )
        for unique_set in known.unique_sets:
            key = names.new(naming.KEY, "_".join(unique_set.columns))
            lines.append(f"unique_set({key}, {definition.id}).")
            # Members carry their position so evidence read back from the model
            # keeps the order the Unique Set was declared in.
            lines.extend(
                f"unique_set_member({key}, {position}, {_text(column)})."
                for position, column in enumerate(unique_set.columns)
            )

    for instance in ir.all_instances(program.root):
        lines.append(f"instance_of({instance.id}, {instance.definition_id}).")

    for project in ir.projects(program.root):
        (input_instance,) = ir.instances(project.input)
        lines.append(f"project_input({project.instance.definition_id}, {input_instance.id}).")
        for output in project.outputs:
            lines.extend(_expression_facts(output.expression))
            lines.append(
                f"project_output({project.instance.definition_id}, {_text(output.name)}, {output.expression.id})."
            )

    for join in ir.joins(program.root):
        lines.append(f"join_kind({join.id}, {join.kind}).")
        lines.extend(f"join_left_instance({join.id}, {instance.id})." for instance in ir.instances(join.left))
        lines.extend(f"join_right_instance({join.id}, {instance.id})." for instance in ir.instances(join.right))
        for equality in join.equalities:
            lines.extend(_expression_facts(equality.left))
            lines.extend(_expression_facts(equality.right))
            lines.append(f"join_equality({join.id}, {equality.left.id}, {equality.right.id}).")

    lines.extend(f"assertion({assertion.id}, {assertion.join_id})." for assertion in program.assertions)

    return "\n".join(lines)


def _expression_facts(expression: ir.Expression) -> list[str]:
    if isinstance(expression, ir.ColumnReference):
        return [f"expression_column({expression.id}, {expression.instance_id}, {_text(expression.column)})."]
    if isinstance(expression, ir.Constant):
        return [f"expression_constant({expression.id})."]
    return [f"expression_opaque({expression.id})."]


def _text(value: str) -> str:
    escaped = value.lower().replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
