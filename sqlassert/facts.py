"""Encode the semantic IR and Knowledge as deterministic ground ASP facts."""

from __future__ import annotations

from dataclasses import dataclass

from sqlassert import ir, naming
from sqlassert.knowledge import Knowledge
from sqlassert.naming import NameGiver


@dataclass(frozen=True)
class ClingoEncoding:
    facts: str
    node_to_symbol: dict[ir.Node, str]
    symbol_to_node: dict[str, ir.Node]


def encode(program: ir.Program, knowledge: Knowledge) -> ClingoEncoding:
    nodes = _walk(program)
    names = NameGiver()
    node_to_symbol = {node: names.new(*_symbol_hint(node)) for node in nodes}
    symbol_to_node = {symbol: node for node, symbol in node_to_symbol.items()}
    lines: list[str] = []

    relations = [node for node in nodes if isinstance(node, ir.RelationExpr)]
    for relation in relations:
        relation_symbol = node_to_symbol[relation]
        lines.append(f"relation({relation_symbol}).")
        for column in relation.outputs:
            lines.append(f"relation_output({relation_symbol}, {node_to_symbol[column]}).")

        if isinstance(relation, ir.NamedRelation):
            if relation.body is not None:
                lines.append(f"property_input({relation_symbol}, {node_to_symbol[relation.body]}).")
            known = knowledge.relation(relation.name) if relation.role is not ir.RelationRole.CTE else None
            if known is not None:
                by_name = {column.name.lower(): column for column in relation.outputs}
                lines.extend(
                    f"column_not_null({relation_symbol}, {node_to_symbol[by_name[column.name.lower()]]})."
                    for column in known.columns
                    if not column.nullable and column.name.lower() in by_name
                )
                for unique_set in known.unique_sets:
                    members = tuple(by_name[column.lower()] for column in unique_set.columns if column.lower() in by_name)
                    if len(members) == len(unique_set.columns):
                        lines.extend(_unique_set_facts(names, node_to_symbol, relation, members))
        elif isinstance(relation, ir.Alias):
            lines.append(f"property_input({relation_symbol}, {node_to_symbol[relation.source]}).")
        elif isinstance(relation, (ir.Filter, ir.Project)):
            lines.append(f"property_input({relation_symbol}, {node_to_symbol[relation.input]}).")
        elif isinstance(relation, ir.Aggregate):
            lines.append(f"aggregate_relation({relation_symbol}).")
            lines.extend(
                f"aggregate_grouping_output({relation_symbol}, {position}, {node_to_symbol[column]})."
                for position, column in enumerate(relation.grouping_outputs)
            )
        elif isinstance(relation, ir.Distinct):
            lines.append(f"distinct_relation({relation_symbol}).")
            lines.extend(
                f"distinct_output({relation_symbol}, {position}, {node_to_symbol[column]})."
                for position, column in enumerate(relation.outputs)
            )
        elif isinstance(relation, ir.QualifyByPartition):
            lines.append(f"qualify_by_partition({relation_symbol}).")
            lines.extend(
                f"qualify_partition_output({relation_symbol}, {position}, {node_to_symbol[column]})."
                for position, column in enumerate(relation.partition_outputs)
            )
        elif isinstance(relation, ir.Join):
            lines.append(f"join_kind({relation_symbol}, {relation.kind}).")
            lines.append(f"join_left({relation_symbol}, {node_to_symbol[relation.left]}).")
            lines.append(f"join_right({relation_symbol}, {node_to_symbol[relation.right]}).")
            for equality in relation.equalities:
                lines.append(
                    f"join_equality({relation_symbol}, {node_to_symbol[equality.left]}, {node_to_symbol[equality.right]})."
                )

    for node in nodes:
        symbol = node_to_symbol[node]
        if isinstance(node, ir.OutputColumn):
            lines.append(f"column_expression({symbol}, {node_to_symbol[node.expression]}).")
        elif isinstance(node, ir.ColumnRef):
            lines.append(f"expression_column({symbol}, {node_to_symbol[node.column]}).")
        elif isinstance(node, ir.Constant):
            lines.append(f"expression_constant({symbol}).")
        elif isinstance(node, ir.OpaqueExpression):
            lines.append(f"expression_opaque({symbol}).")
        elif isinstance(node, ir.UniqueJoinAssertion):
            lines.append(f"assertion({symbol}, {node_to_symbol[node.subject]}).")
        elif isinstance(node, ir.UniqueSetAssertion):
            lines.append(f"unique_set_assertion({symbol}, {node_to_symbol[node.subject]}).")
            if node.candidate_key:
                lines.append(f"unique_set_assertion_key({symbol}).")
            lines.extend(
                f"unique_set_assertion_member({symbol}, {position}, {node_to_symbol[column]})."
                for position, column in enumerate(node.columns)
            )

    return ClingoEncoding("\n".join(lines), node_to_symbol, symbol_to_node)


def _unique_set_facts(names: NameGiver, symbols: dict[ir.Node, str], relation: ir.RelationExpr, columns: tuple[ir.OutputColumn, ...]) -> list[str]:
    key = names.new(naming.KEY, "_".join(column.name for column in columns))
    return [f"unique_set({key}, {symbols[relation]})."] + [
        f"unique_set_member({key}, {position}, {symbols[column]})." for position, column in enumerate(columns)
    ]


def _walk(program: ir.Program) -> tuple[ir.Node, ...]:
    found: list[ir.Node] = []
    seen: set[int] = set()

    def visit(node: ir.Node | None) -> None:
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        found.append(node)
        for child in ir.children(node):
            visit(child)

    for declaration in program.declarations:
        visit(declaration)
    visit(program.root)
    for assertion in program.assertions:
        visit(assertion)
    return tuple(found)


def _symbol_hint(node: ir.Node) -> tuple[str, str]:
    if isinstance(node, ir.OutputColumn):
        return naming.COLUMN, node.name
    if isinstance(node, ir.NamedRelation):
        return naming.RELATION, node.name
    if isinstance(node, ir.Alias):
        return naming.RELATION, node.name
    if isinstance(node, ir.Join):
        return naming.JOIN, node.kind
    if isinstance(node, ir.RelationExpr):
        return naming.RELATION, type(node).__name__
    if isinstance(node, ir.Assertion):
        return naming.ASSERTION, type(node).__name__
    return naming.EXPRESSION, type(node).__name__
