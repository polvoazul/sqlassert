"""Map dbt data tests and model contract constraints onto Properties.

An Expectation is one test or constraint read as a Property of the relation it
is attached to. It is used twice: as an Assertion about its own model (can
analysis prove the test passes?) and, for every model downstream, as a
Property Declaration (the test ran and passed before they were built). The
two can differ: `dbt_utils.unique_combination_of_columns` counts NULL equal
to NULL, so it is proved by a Non-Null Unique Set but declares only a Unique
Set; a test with a `where:` config is proved by the Property on every row but
declares nothing, since it checked only some rows.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Literal

import sqlglot
from sqlglot.errors import SqlglotError

from sqlassert.dbt.manifest import Constraint, Manifest, Relation, Test
from sqlassert.ir.model import LiteralValue
from sqlassert.sql_parse import literal_value

# How an Expectation enters analysis: "unique" a Unique Set, "key" a Non-Null
# Unique Set, "not_null" Not Null, "enum" an Enumerated Domain, "coverage"
# Referential Coverage (a dbt relationships test checks no Unique Set on its target).
type PropertyKind = Literal["unique", "key", "not_null", "enum", "coverage"]

# Constraint types each adapter enforces when a contracted table is built
# (dbt's per-adapter `CONSTRAINT_SUPPORT`). Every other adapter enforces
# NOT NULL only; their keys are informational. Constraints never apply to views.
_ENFORCED_CONSTRAINTS = {
    "postgres": frozenset({"not_null", "unique", "primary_key", "foreign_key"}),
    "duckdb": frozenset({"not_null", "unique", "primary_key", "foreign_key"}),
}
_NOT_NULL_ONLY = frozenset({"not_null"})
_CONSTRAINED_MATERIALIZATIONS = {"table", "incremental"}

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_QUOTED = re.compile(r'"([^"]+)"|`([^`]+)`|\[([^\]]+)\]')
_CALL = re.compile(r"\b(ref|source)\s*\(.*?\)", re.DOTALL)
_THRESHOLD = re.compile(r"\s*(!=|<>|>=|<=|==|=|>|<)\s*(\d+)\s*")


@dataclass(frozen=True)
class Expectation:
    """One test or constraint as a Property of relation `node`.

    `asserted` is the Property whose proof proves it passes, None when none
    does (`unprovable` says why). `declared` is the Property it guarantees
    downstream once it passed, None when it is no fact (`not_a_fact` says why).
    """

    unique_id: str
    name: str
    kind: Literal["test", "constraint"]
    node: str
    path: str
    asserted: PropertyKind | None
    declared: PropertyKind | None
    columns: tuple[str, ...] = ()
    values: tuple[LiteralValue, ...] = ()
    referenced: str | None = None
    referenced_columns: tuple[str, ...] = ()
    unprovable: str | None = None
    not_a_fact: str | None = None

    @property
    def is_fact(self) -> bool:
        return self.declared is not None and self.not_a_fact is None


def expectations(manifest: Manifest) -> Iterator[Expectation]:
    for test in manifest.tests.values():
        expectation = _test(test, manifest.relations)
        if expectation is not None:
            yield expectation
    for relation in manifest.relations.values():
        yield from _constraints(relation, manifest)


def _test(test: Test, relations: Mapping[str, Relation]) -> Expectation | None:
    node = _attached(test, relations)
    if node is None:
        return None  # A test of nothing analysis knows: a seed-less singular test, say.
    base = Expectation(test.unique_id, test.name, "test", node, test.original_file_path, asserted=None, declared=None)
    weakness = _weakness(test)
    unprovable = _threshold_unprovable(test)
    name = test.test_name if test.namespace is None else f"{test.namespace}.{test.test_name}"
    match name:
        case None:
            return replace(base, asserted=None, declared=None, unprovable="a singular test states no Property")
        case "unique" | "not_null" | "accepted_values" | "relationships":
            column = _column(test.kwargs.get("column_name") or test.column_name)
            if column is None:
                return replace(
                    base,
                    unprovable=f"it tests the expression {test.kwargs.get('column_name') or test.column_name!r}, not a column",
                )
        case "dbt_utils.unique_combination_of_columns":
            names = test.kwargs.get("combination_of_columns")
            columns = tuple(_column(name) for name in names) if isinstance(names, list) and names else (None,)
            if None in columns:
                return replace(base, asserted=None, declared=None, unprovable="its columns are not all plain columns")
            return replace(
                base, asserted="key", declared="unique", columns=tuple(c for c in columns if c is not None),
                unprovable=unprovable, not_a_fact=weakness,
            )
        case _:
            return replace(base, asserted=None, declared=None, unprovable=f"the {name} test maps to no Property")
    match name:
        case "unique":
            return replace(base, asserted="unique", declared="unique", columns=(column,), unprovable=unprovable, not_a_fact=weakness)
        case "not_null":
            return replace(base, asserted="not_null", declared="not_null", columns=(column,), unprovable=unprovable, not_a_fact=weakness)
        case "accepted_values":
            values = _accepted_values(test.kwargs.get("values"), test.kwargs.get("quote", True))
            if values is None:
                return replace(base, asserted=None, declared=None, unprovable="its values are not all string, numeric or boolean literals")
            return replace(
                base, asserted="enum", declared="enum", columns=(column,), values=values, unprovable=unprovable, not_a_fact=weakness,
            )
        case _:
            target = _resolve_call(test.kwargs.get("to"), test.depends_on, relations)
            field = _column(test.kwargs.get("field"))
            if target is None or field is None:
                return replace(base, asserted=None, declared=None, unprovable="its `to` or `field` cannot be resolved")
            return replace(
                base, asserted="coverage", declared="coverage", columns=(column,), referenced=target, referenced_columns=(field,),
                unprovable=unprovable, not_a_fact=weakness,
            )


def _attached(test: Test, relations: Mapping[str, Relation]) -> str | None:
    if test.attached_node in relations:
        return test.attached_node
    # A source test has no attached node; its model argument names the source.
    attached = _resolve_call(test.kwargs.get("model"), test.depends_on, relations)
    if attached is not None:
        return attached
    candidates = [node for node in test.depends_on if node in relations]
    return candidates[0] if len(candidates) == 1 else None


def _weakness(test: Test) -> str | None:
    """Why a passing test is no fact about every row, or None when it is one."""
    if test.severity != "error":
        return f"its severity is {test.severity}, so a failure does not stop the build"
    if test.where is not None:
        return f"it checks only the rows where {test.where}"
    if _normalized(test.fail_calc) != "count(*)" or not _fails_on_any_failure(test.error_if):
        return f"its thresholds (fail_calc {test.fail_calc}, error_if {test.error_if}) tolerate failing rows"
    return None


def _threshold_unprovable(test: Test) -> str | None:
    """Why proving no row fails does not prove the test passes, or None."""
    if _normalized(test.fail_calc) != "count(*)":
        return f"it computes failures with fail_calc {test.fail_calc}"
    for condition in (test.warn_if, test.error_if):
        if _compare(0, condition) is not False:
            return f"its threshold {condition} is not met by zero failing rows"
    return None


def _fails_on_any_failure(condition: str) -> bool:
    """Whether `condition` errors on one failing row and on every count above it."""
    match = _THRESHOLD.fullmatch(condition)
    return (
        match is not None and match.group(1) in {"!=", "<>", ">", ">="}
        and _compare(0, condition) is False and _compare(1, condition) is True
    )


def _compare(failures: int, condition: str) -> bool | None:
    match = _THRESHOLD.fullmatch(condition)
    if match is None:
        return None
    operator, bound = match.group(1), int(match.group(2))
    return {
        "!=": failures != bound, "<>": failures != bound, ">": failures > bound, ">=": failures >= bound,
        "<": failures < bound, "<=": failures <= bound, "=": failures == bound, "==": failures == bound,
    }[operator]


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _column(name: object) -> str | None:
    """A plain or quoted column name; None for an expression."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if _IDENTIFIER.fullmatch(name):
        return name
    quoted = _QUOTED.fullmatch(name)
    return next((group for group in quoted.groups() if group), None) if quoted else None


def _accepted_values(values: object, quote: object) -> tuple[LiteralValue, ...] | None:
    """The literals dbt's accepted_values test compares with: each value quoted
    as a string, or with `quote: false`, its text read as a SQL literal."""
    if not isinstance(values, list) or not values:
        return None
    found: list[LiteralValue] = []
    for value in values:
        text = _jinja_text(value)
        if quote is not False:
            literal = LiteralValue(type="string", text=text)
        else:
            try:
                literal = literal_value(sqlglot.parse_one(text))
            except SqlglotError:
                return None
            if literal is None:
                return None
        if literal not in found:
            found.append(literal)
    return tuple(found)


def _jinja_text(value: object) -> str:
    """How Jinja prints a YAML value inside `'{{ value }}'`."""
    return str(value)


def _resolve_call(text: object, depends_on: tuple[str, ...], relations: Mapping[str, Relation]) -> str | None:
    """The relation a `ref(...)` or `source(...)` call written in `text` names,
    looked up among `depends_on` only: dbt resolved it there."""
    if not isinstance(text, str):
        return None
    match = _CALL.search(text)
    if match is None:
        return None
    try:
        call = ast.parse(match.group(0), mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(call, ast.Call) or not all(isinstance(argument, ast.Constant) for argument in call.args):
        return None
    arguments = tuple(str(argument.value) for argument in call.args if isinstance(argument, ast.Constant))
    options: dict[str, object] = {
        keyword.arg: keyword.value.value for keyword in call.keywords
        if keyword.arg is not None and isinstance(keyword.value, ast.Constant)
    }
    return resolve(match.group(1), arguments, options, depends_on, relations)


def resolve(
    function: str, arguments: tuple[str, ...], options: Mapping[str, object], depends_on: tuple[str, ...],
    relations: Mapping[str, Relation],
) -> str | None:
    """The unique id among `depends_on` a `ref` or `source` call resolves to, or None."""
    candidates = [relations[node] for node in depends_on if node in relations]
    match function, arguments:
        case "source", (source_name, name):
            found = [r for r in candidates if r.resource_type == "source" and r.source_name == source_name and r.name == name]
        case "ref", (name,) | (_, name):
            package = arguments[0] if len(arguments) == 2 else None
            version = options.get("v", options.get("version"))
            found = [
                r for r in candidates
                if r.resource_type != "source" and r.name == name and (package is None or r.package_name == package)
                and (version is None or r.version == str(version))
            ]
        case _:
            return None
    return found[0].unique_id if len(found) == 1 else None


def _constraints(relation: Relation, manifest: Manifest) -> Iterator[Expectation]:
    enforced = _ENFORCED_CONSTRAINTS.get(manifest.adapter_type or "", _NOT_NULL_ONLY)
    for index, constraint in enumerate(relation.constraints):
        kind: PropertyKind
        match constraint.type:
            case "not_null":
                kind = "not_null"
            case "unique":
                kind = "unique"
            case "primary_key":
                kind = "key"
            case "foreign_key":
                kind = "coverage"
            case _:
                continue  # A check or custom constraint states no Property.
        if not relation.contract_enforced:
            not_a_fact = "the model's contract is not enforced"
        elif relation.materialized not in _CONSTRAINED_MATERIALIZATIONS:
            not_a_fact = f"constraints are not applied to a {relation.materialized} materialization"
        elif constraint.type not in enforced:
            not_a_fact = f"{manifest.adapter_type} does not enforce {constraint.type} constraints"
        else:
            not_a_fact = None
        base = Expectation(
            f"{relation.unique_id}.constraint.{index}",
            f"{constraint.type} constraint on {relation.name}({', '.join(constraint.columns)})",
            "constraint", relation.unique_id, relation.patch_path or relation.original_file_path, asserted=None, declared=None,
        )
        if not constraint.columns:
            yield replace(base, asserted=None, declared=None, unprovable="it names no columns")
            continue
        if kind != "coverage":
            yield replace(base, asserted=kind, declared=kind, columns=constraint.columns, not_a_fact=not_a_fact)
            continue
        target, target_columns = _foreign_key_target(constraint, relation, manifest)
        if target is None or len(target_columns) != len(constraint.columns):
            yield replace(base, asserted=None, declared=None, unprovable="its referenced relation or columns cannot be resolved")
            continue
        yield replace(
            base, asserted=kind, declared=kind, columns=constraint.columns, referenced=target, referenced_columns=target_columns,
            not_a_fact=not_a_fact,
        )


def _foreign_key_target(constraint: Constraint, relation: Relation, manifest: Manifest) -> tuple[str | None, tuple[str, ...]]:
    if constraint.to is not None:
        return _resolve_call(constraint.to, relation.depends_on, manifest.relations) or _resolve_call(
            constraint.to, tuple(manifest.relations), manifest.relations
        ), constraint.to_columns
    # An older foreign key's expression is raw SQL: `relation (column, ...)`.
    match = re.fullmatch(r"\s*(.+?)\s*\(([^()]*)\)\s*", constraint.expression or "")
    if match is None:
        return None, ()
    name = _canonical(match.group(1))
    found = [r.unique_id for r in manifest.relations.values() if r.relation_name is not None and _canonical(r.relation_name) == name]
    columns = tuple(column for part in match.group(2).split(",") if (column := _column(part)) is not None)
    return (found[0] if len(found) == 1 else None), columns


def _canonical(name: str) -> str:
    return re.sub(r'["`\[\]\s]', "", name).lower()
