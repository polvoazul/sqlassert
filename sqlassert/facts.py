"""Encode the semantic IR and accepted Properties as deterministic Clingo input."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

import case_conversion

from sqlassert import ir, naming
from sqlassert.properties import CandidateKey, Property, UniqueJoin, UniqueSet
from sqlassert.naming import NameGiver

type Symbols = dict[ir.Node | Property, str]


@dataclass(frozen=True)
class ClingoEncoding:
    inheritance_rules: str
    facts: str
    node_to_symbol: dict[ir.Node, str]
    symbol_to_node: dict[str, ir.Node]


EXCEPTIONS: dict[type[ir.Node], Callable[[ir.Node, str, Symbols], Iterable[str]]] = {
    ir.Node: lambda node, symbol, symbols: (),  # Node.origin remains outside Clingo.
    ir.Assertion: lambda node, symbol, symbols: _assertion_property_facts(node, symbol, symbols),
}


def encode(program: ir.Program, knowledge: tuple[Property, ...]) -> ClingoEncoding:
    nodes = _walk(program)
    public_properties = (*knowledge, *program.declarations)
    names = NameGiver()
    node_to_symbol = {node: names.new(*_symbol_hint(node)) for node in nodes}
    property_to_symbol = {item: names.new(naming.PROPERTY, _class_name(type(item))) for item in public_properties}
    symbols = node_to_symbol | property_to_symbol
    symbol_to_node = {symbol: node for node, symbol in node_to_symbol.items()}
    lines = _ir_facts(nodes, symbols)
    lines.extend(_public_facts(public_properties, symbols))
    return ClingoEncoding("\n".join(_inheritance_rules()), "\n".join(lines), node_to_symbol, symbol_to_node)


def _ir_facts(nodes: tuple[ir.Node, ...], symbols: Symbols) -> list[str]:
    """Generic IR tree encoder. For each reachable node, it emits:

    ```scss
    ir__filter(f1).
    ir__filter__input(f1, source1).
    ir__relation_expr__output_columns(f1, 0, c1).
    ```
    """
    lines: list[str] = []
    for node in nodes:
        symbol = symbols[node]
        lines.append(f"ir__{_class_name(type(node))}({symbol}).")
        for owner in reversed(type(node).__mro__):
            if not issubclass(owner, ir.Node):
                continue
            exceptional_case = EXCEPTIONS.get(owner)
            if exceptional_case is not None:
                lines.extend(exceptional_case(node, symbol, symbols))
                continue
            for field_name in inspect.get_annotations(owner, eval_str=False):
                lines.extend(_field_facts("ir", owner, field_name, symbol, getattr(node, field_name), symbols))
    return lines


def _assertion_property_facts(assertion: ir.Node, assertion_symbol: str, symbols: Symbols) -> list[str]:
    if not isinstance(assertion, ir.Assertion):
        raise TypeError("Assertion.property encoding requires an Assertion")
    match assertion.property:
        case CandidateKey(columns=columns):
            kind = "candidate_key"
            join = None
        case UniqueSet(columns=columns):
            kind = "unique_set"
            join = None
        case UniqueJoin(join=join):
            kind = "unique_join"
            columns = frozenset()
        case property:
            raise TypeError(f"cannot encode {type(property).__name__} as an assertion query")
    lines = [f"ir__assertion__property({assertion_symbol}, {kind})."]
    lines.extend(
        f"ir__assertion__columns({assertion_symbol}, {_term(column, symbols)})."
        for column in sorted(columns, key=lambda column: symbols[column])
    )
    if join is not None:
        lines.append(f"ir__assertion__join({assertion_symbol}, {_term(join, symbols)}).")
    return lines


def _public_facts(properties: tuple[Property, ...], symbols: Symbols) -> list[str]:
    lines: list[str] = []
    for item in properties:
        symbol = symbols[item]
        lines.append(f"pub__{_class_name(type(item))}({symbol}).")
        for owner in reversed(type(item).__mro__):
            if not issubclass(owner, Property):
                continue
            for field_name in inspect.get_annotations(owner, eval_str=False):
                lines.extend(_field_facts("pub", owner, field_name, symbol, getattr(item, field_name), symbols))
    return lines


def _field_facts(namespace: str, owner: type, field_name: str, symbol: str, value: object, symbols: Symbols) -> list[str]:
    predicate = f"{namespace}__{_class_name(owner)}__{field_name}"
    match value:
        case None | False:
            return []
        case True:
            return [f"{predicate}({symbol})."]
        case frozenset() as values:
            return [f"{predicate}({symbol}, {term})." for term in sorted(_term(item, symbols) for item in values)]
        case tuple() | list() as values:
            return [f"{predicate}({symbol}, {position}, {_term(item, symbols)})." for position, item in enumerate(values)]
        case _:
            return [f"{predicate}({symbol}, {_term(value, symbols)})."]


def _term(value: object, symbols: Symbols) -> str:
    match value:
        case ir.Node() | Property():
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


def _inheritance_rules() -> list[str]:
    rules: list[str] = []
    for node_type in _node_types():
        for base in node_type.__bases__:
            if issubclass(base, ir.Node):
                rules.append(f"ir__{_class_name(base)}(Node) :- ir__{_class_name(node_type)}(Node).")
    for property_type in _property_types():
        for base in property_type.__bases__:
            if issubclass(base, Property):
                rules.append(f"pub__{_class_name(base)}(Property) :- pub__{_class_name(property_type)}(Property).")
    return rules


def _node_types() -> tuple[type[ir.Node], ...]:
    found: set[type[ir.Node]] = set()
    pending = list(ir.Node.__subclasses__())
    while pending:
        node_type = pending.pop()
        found.add(node_type)
        pending.extend(node_type.__subclasses__())
    return tuple(sorted(found, key=_class_name))


def _property_types() -> tuple[type[Property], ...]:
    found: set[type[Property]] = set()
    pending = list(Property.__subclasses__())
    while pending:
        property_type = pending.pop()
        found.add(property_type)
        pending.extend(property_type.__subclasses__())
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

    for named_relation in program.named_relations:
        visit(named_relation)
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


def _class_name(node_type: type[object]) -> str:
    return case_conversion.snake(node_type.__name__)
