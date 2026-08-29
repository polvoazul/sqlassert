"""Encode the semantic IR and public Knowledge as deterministic Clingo input."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

import case_conversion

from sqlassert import ir, naming
from sqlassert.knowledge import Knowledge
from sqlassert.naming import NameGiver


@dataclass(frozen=True)
class ClingoEncoding:
    inheritance_rules: str
    facts: str
    node_to_symbol: dict[ir.Node, str]
    symbol_to_node: dict[str, ir.Node]


EXCEPTIONS: dict[type[ir.Node], Callable[[ir.Node, str], Iterable[str]]] = {
    ir.Node: lambda node, symbol: (),  # Node.origin remains outside Clingo.
}

def encode(program: ir.Program, knowledge: Knowledge) -> ClingoEncoding:
    nodes = _walk(program)
    names = NameGiver()
    node_to_symbol = {node: names.new(*_symbol_hint(node)) for node in nodes}
    symbol_to_node = {symbol: node for node, symbol in node_to_symbol.items()}
    lines = _ir_facts(nodes, node_to_symbol)
    lines.extend(_knowledge_facts(knowledge, nodes, node_to_symbol, names))
    return ClingoEncoding("\n".join(_inheritance_rules()), "\n".join(lines), node_to_symbol, symbol_to_node)


def _ir_facts(nodes: tuple[ir.Node, ...], symbols: dict[ir.Node, str]) -> list[str]:
    """Generic IR tree encoder. For each reachable node, it emits:

    ```scss
    ir__filter(f1).
    ir__filter__input(f1, source1).
    ir__relation_expr__output_columns(f1, 0, c1).
    ```
    """
    lines: list[str] = []
    for node in nodes:
        node_type = type(node)
        symbol = symbols[node]
        lines.append(f"ir__{_class_name(node_type)}({symbol}).")
        for owner in reversed(node_type.__mro__):
            assert isinstance(owner, type) and issubclass(owner, ir.Node)
            exceptional_case = EXCEPTIONS.get(owner)
            if exceptional_case:
                lines.extend(exception(node, symbol))
                continue
            for field_name in inspect.get_annotations(owner, eval_str=False):
                lines.extend(_field_facts(owner, field_name, symbol, getattr(node, field_name), symbols))
    return lines


def _field_facts(owner: type[ir.Node], field_name: str, symbol: str, value: object, symbols: dict[ir.Node, str]) -> list[str]:
    predicate = f"ir__{_class_name(owner)}__{field_name}"
    match value:
        case None | False:
            return []
        case True:
            return [f"{predicate}({symbol})."]
        case tuple() as values:
            return [f"{predicate}({symbol}, {position}, {_term(item, symbols)})." for position, item in enumerate(values)]
        case _:
            return [f"{predicate}({symbol}, {_term(value, symbols)})."]


def _term(value: object, symbols: dict[ir.Node, str]) -> str:
    match value:
        case ir.Node():
            return symbols[value]
        case Enum() as enum:
            atom = enum.name.lower()
            if atom == case_conversion.snake(atom):
                return atom
        case str() as text:
            return json.dumps(text)
        case int() as number:
            return str(number)
    raise TypeError(f"cannot encode {type(value).__name__} as a Clingo term")


def _knowledge_facts(
    knowledge: Knowledge, nodes: tuple[ir.Node, ...], symbols: dict[ir.Node, str], names: NameGiver
) -> list[str]:
    lines: list[str] = []
    for relation in nodes:
        match relation:
            case ir.NamedRelation(role=ir.RelationRole.CTE):
                continue
            case ir.NamedRelation() as named_relation:
                pass
            case _:
                continue
        known = knowledge.relation(named_relation.name)
        if known is None:
            continue
        by_name = {column.name.lower(): column for column in named_relation.output_columns}
        lines.extend(
            f"pub__non_null_column({symbols[named_relation]}, {symbols[by_name[column.name.lower()]]})."
            for column in known.columns
            if not column.nullable and column.name.lower() in by_name
        )
        for unique_set in known.unique_sets:
            columns = tuple(by_name[column.lower()] for column in unique_set.columns if column.lower() in by_name)
            if len(columns) == len(unique_set.columns):
                lines.extend(_unique_set_facts(names, symbols, named_relation, columns))
    return lines


def _unique_set_facts(
    names: NameGiver, symbols: dict[ir.Node, str], relation: ir.RelationExpr, columns: tuple[ir.OutputColumn, ...]
) -> list[str]:
    key = names.new(naming.KEY, "_".join(column.name for column in columns))
    return [f"pub__unique_set({key}, {symbols[relation]})."] + [
        f"pub__unique_set_column({key}, {position}, {symbols[column]})." for position, column in enumerate(columns)
    ]


def _inheritance_rules() -> list[str]:
    rules: list[str] = []
    for node_type in _node_types():
        for base in node_type.__bases__:
            if issubclass(base, ir.Node):
                rules.append(f"ir__{_class_name(base)}(Node) :- ir__{_class_name(node_type)}(Node).")
    return rules


def _node_types() -> tuple[type[ir.Node], ...]:
    found: set[type[ir.Node]] = set()
    pending = list(ir.Node.__subclasses__())
    while pending:
        node_type = pending.pop()
        found.add(node_type)
        pending.extend(node_type.__subclasses__())
    return tuple(sorted(found, key=_class_name))


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
    match node:
        case ir.OutputColumn(name=name):
            return naming.COLUMN, name
        case ir.NamedRelation(name=name) | ir.Alias(name=name):
            return naming.RELATION, name
        case ir.Join(kind=kind):
            return naming.JOIN, kind
        case ir.RelationExpr():
            return naming.RELATION, type(node).__name__
        case ir.Assertion():
            return naming.ASSERTION, type(node).__name__
        case _:
            return naming.EXPRESSION, type(node).__name__


def _class_name(node_type: type[ir.Node]) -> str:
    return case_conversion.snake(node_type.__name__)
