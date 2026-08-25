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
            lines.extend(_unique_set_facts(names, definition.id, unique_set.columns))

    for instance in ir.all_instances(program.root):
        lines.append(f"instance_of({instance.id}, {instance.definition_id}).")

    for filtered in ir.filters(program.root):
        (input_instance,) = ir.instances(filtered.input)
        lines.append(f"filter_input({filtered.instance.definition_id}, {input_instance.id}).")

    for project in ir.projects(program.root):
        (input_instance,) = ir.instances(project.input)
        lines.append(f"project_input({project.instance.definition_id}, {input_instance.id}).")
        for output in project.outputs:
            lines.extend(_expression_facts(output.expression))
            lines.append(
                f"project_output({project.instance.definition_id}, {_text(output.name)}, {output.expression.id})."
            )

    # An Aggregate or Distinct earns its own Unique Set directly from its
    # Grouping Keys or output columns -- unlike Project, it needs no
    # propagation rule, since grouping or distinctness alone establishes it
    # regardless of the input relation's own Unique Sets.
    for aggregate in ir.aggregates(program.root):
        columns = tuple(key.name for key in aggregate.grouping_keys)
        lines.extend(_unique_set_facts(names, aggregate.instance.definition_id, columns))

    for distinct in ir.distincts(program.root):
        columns = tuple(output.name for output in distinct.outputs)
        lines.extend(_unique_set_facts(names, distinct.instance.definition_id, columns))

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


def _unique_set_facts(names: NameGiver, definition_id: str, columns: tuple[str, ...]) -> list[str]:
    key = names.new(naming.KEY, "_".join(columns))
    # Members carry their position so evidence read back from the model keeps
    # the order the Unique Set's columns were declared in.
    return [f"unique_set({key}, {definition_id})."] + [
        f"unique_set_member({key}, {position}, {_text(column)})." for position, column in enumerate(columns)
    ]


def _expression_facts(expression: ir.Expression) -> list[str]:
    if isinstance(expression, ir.ColumnReference):
        return [f"expression_column({expression.id}, {expression.instance_id}, {_text(expression.column)})."]
    if isinstance(expression, ir.Constant):
        return [f"expression_constant({expression.id})."]
    return [f"expression_opaque({expression.id})."]


def _text(value: str) -> str:
    escaped = value.lower().replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
