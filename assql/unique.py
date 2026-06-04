from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType, Tokenizer


FALSE_ASSERTION = "select false"
MARKER = re.compile(re.escape(r"/**unique**/"), flags=re.IGNORECASE)


@dataclass(frozen=True)
class UniqueJoinCheckResult:
    marker_index: int
    valid: bool
    reason: str
    inferred_key_columns: tuple[str, ...] = ()
    constrained_key_columns: tuple[str, ...] = ()
    unique_constraints: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class UniqueJoinValidationResult:
    valid: bool
    reason: str
    checks: tuple[UniqueJoinCheckResult, ...] = ()


@dataclass(frozen=True)
class _UniqueJoinPlan:
    marker_index: int
    reason: str
    join_sql: str = ""
    rhs: exp.Expression | None = None
    rhs_names: frozenset[str] = frozenset()
    keys: tuple[exp.Column, ...] = ()
    rhs_filter_columns: tuple[exp.Column, ...] = ()


def validate_unique_joins(
    connection: Any,
    sql: str,
    dialect: str = "duckdb",
) -> UniqueJoinValidationResult:
    plans = _unique_join_plans(sql, dialect)
    if not plans:
        return UniqueJoinValidationResult(True, "no unique join markers found")

    checks = tuple(_validate_plan(connection, plan) for plan in plans)
    if all(check.valid for check in checks):
        return UniqueJoinValidationResult(True, "all unique join assertions passed", checks)

    reason = "; ".join(check.reason for check in checks if not check.valid)
    return UniqueJoinValidationResult(False, reason, checks)


def unique_assertions(sql: str, dialect: str = "duckdb") -> list[str]:
    return [FALSE_ASSERTION for _ in _unique_join_plans(sql, dialect)]


def _unique_join_plans(sql: str, dialect: str) -> list[_UniqueJoinPlan]:
    marker_join_indexes = _marked_join_indexes(sql, dialect)
    if not marker_join_indexes:
        return []

    try:
        expressions = [expression for expression in sqlglot.parse(sql, read=dialect) if expression]
    except SqlglotError:
        return [
            _UniqueJoinPlan(index, "SQL parse failed")
            for index, _ in enumerate(marker_join_indexes)
        ]

    joins: list[exp.Join] = []
    for expression in expressions:
        joins.extend(expression.find_all(exp.Join))

    plans: list[_UniqueJoinPlan] = []
    for marker_index, join_index in enumerate(marker_join_indexes):
        if join_index is None or join_index >= len(joins):
            plans.append(_UniqueJoinPlan(marker_index, "marker is not followed by a join"))
            continue
        plans.append(_plan_for_join(marker_index, joins[join_index], dialect))
    return plans


def _marked_join_indexes(sql: str, dialect: str) -> list[int | None]:
    marker_offsets = [match.end() for match in MARKER.finditer(sql)]
    if not marker_offsets:
        return []

    tokens = Tokenizer(dialect=dialect).tokenize(sql)
    join_offsets = [
        token.start
        for token in tokens
        if token.token_type is TokenType.JOIN
    ]

    indexes: list[int | None] = []
    for marker_offset in marker_offsets:
        next_join_index = next(
            (index for index, join_offset in enumerate(join_offsets) if join_offset >= marker_offset),
            None,
        )
        indexes.append(next_join_index)
    return indexes


def _plan_for_join(marker_index: int, join: exp.Join, dialect: str) -> _UniqueJoinPlan:
    join_sql = _join_sql(join, dialect)
    kind = (join.args.get("kind") or "").upper()
    if kind in {"ANTI", "SEMI"}:
        return _UniqueJoinPlan(
            marker_index,
            f"in join {join_sql}, {kind.lower()} joins are not supported",
            join_sql,
        )

    rhs = join.this
    on = join.args.get("on")
    if rhs is None or on is None:
        return _UniqueJoinPlan(marker_index, f"in join {join_sql}, marked join has no ON predicate", join_sql)

    rhs_names = _rhs_names(rhs)
    if not rhs_names:
        return _UniqueJoinPlan(
            marker_index,
            f"in join {join_sql}, could not identify RHS relation name or alias",
            join_sql,
        )

    keys = _rhs_key_columns(on, rhs_names)
    if not keys:
        return _UniqueJoinPlan(
            marker_index,
            f"in join {join_sql}, could not infer RHS key columns from join predicate",
            join_sql,
        )

    return _UniqueJoinPlan(
        marker_index=marker_index,
        reason="join predicate inferred",
        join_sql=join_sql,
        rhs=rhs,
        rhs_names=frozenset(rhs_names),
        keys=tuple(keys),
        rhs_filter_columns=tuple(_rhs_filter_columns(on, rhs_names)),
    )


def _validate_plan(connection: Any, plan: _UniqueJoinPlan) -> UniqueJoinCheckResult:
    key_names = tuple(key.name for key in plan.keys)
    if plan.rhs is None or not plan.keys:
        return UniqueJoinCheckResult(
            marker_index=plan.marker_index,
            valid=False,
            reason=plan.reason,
            inferred_key_columns=key_names,
        )

    if not isinstance(plan.rhs, exp.Table):
        return UniqueJoinCheckResult(
            marker_index=plan.marker_index,
            valid=False,
            reason=_cannot_prove_reason(plan),
            inferred_key_columns=key_names,
        )

    unique_constraints = _unique_constraints(connection, plan.rhs)
    if not unique_constraints:
        return UniqueJoinCheckResult(
            marker_index=plan.marker_index,
            valid=False,
            reason=_cannot_prove_reason(plan),
            inferred_key_columns=key_names,
            unique_constraints=unique_constraints,
        )

    covered_columns = _covered_rhs_column_names(plan)
    for constraint in unique_constraints:
        if all(column.lower() in covered_columns for column in constraint):
            constrained_key_columns = tuple(constraint)
            return UniqueJoinCheckResult(
                marker_index=plan.marker_index,
                valid=True,
                reason=(
                    f"RHS unique constraint ({', '.join(constrained_key_columns)}) "
                    "is covered by inferred keys/filters "
                    f"({', '.join(sorted(covered_columns))})"
                ),
                inferred_key_columns=key_names,
                constrained_key_columns=constrained_key_columns,
                unique_constraints=unique_constraints,
            )

    return UniqueJoinCheckResult(
        marker_index=plan.marker_index,
        valid=False,
        reason=_cannot_prove_reason(plan),
        inferred_key_columns=key_names,
        unique_constraints=unique_constraints,
    )


def _cannot_prove_reason(plan: _UniqueJoinPlan) -> str:
    rhs_columns = _format_columns(tuple(key.name for key in plan.keys))
    verb = "is" if len(plan.keys) == 1 else "are"
    return f"in join {plan.join_sql}, we can't prove that RHS {rhs_columns} {verb} unique"


def _join_sql(join: exp.Join, dialect: str) -> str:
    join_sql = join.sql(dialect=dialect)
    join_sql = re.sub(r"/\*\s*\*?\s*unique\s*\*?\s*\*/", "", join_sql, flags=re.IGNORECASE)
    return " ".join(join_sql.split())


def _rhs_names(rhs: exp.Expression) -> set[str]:
    if rhs.alias:
        return {rhs.alias.lower()}
    if isinstance(rhs, exp.Table):
        return {rhs.name.lower()}
    if rhs.alias_or_name:
        return {rhs.alias_or_name.lower()}
    return set()


def _rhs_key_columns(on: exp.Expression, rhs_names: set[str]) -> list[exp.Column]:
    keys: list[exp.Column] = []
    seen: set[tuple[str, str]] = set()

    for equality in on.find_all(exp.EQ):
        rhs_column = _simple_rhs_column(equality.this, equality.expression, rhs_names)
        if rhs_column is None:
            rhs_column = _simple_rhs_column(equality.expression, equality.this, rhs_names)
        if rhs_column is None:
            continue

        key = (_column_table(rhs_column).lower(), rhs_column.name.lower())
        if key not in seen:
            keys.append(rhs_column.copy())
            seen.add(key)

    return keys


def _simple_rhs_column(
    maybe_column: exp.Expression,
    other_side: exp.Expression,
    rhs_names: set[str],
) -> exp.Column | None:
    if not isinstance(maybe_column, exp.Column):
        return None
    if not _is_rhs_column(maybe_column, rhs_names):
        return None
    if any(_is_rhs_column(column, rhs_names) for column in other_side.find_all(exp.Column)):
        return None
    return maybe_column


def _rhs_filter_columns(on: exp.Expression, rhs_names: set[str]) -> list[exp.Column]:
    filters: list[exp.Column] = []
    seen: set[tuple[str, str]] = set()

    for predicate in _and_terms(on):
        columns = list(predicate.find_all(exp.Column))
        if not columns or not all(_is_rhs_column(column, rhs_names) for column in columns):
            continue
        for column in columns:
            key = (_column_table(column).lower(), column.name.lower())
            if key not in seen:
                filters.append(column.copy())
                seen.add(key)

    return filters


def _and_terms(expression: exp.Expression) -> Iterable[exp.Expression]:
    if isinstance(expression, exp.And):
        yield from _and_terms(expression.this)
        yield from _and_terms(expression.expression)
    else:
        yield expression


def _is_rhs_column(column: exp.Column, rhs_names: set[str]) -> bool:
    table = _column_table(column)
    return bool(table and table.lower() in rhs_names)


def _column_table(column: exp.Column) -> str:
    table = column.table
    return table if isinstance(table, str) else ""


def _covered_rhs_column_names(plan: _UniqueJoinPlan) -> set[str]:
    return {
        column.name.lower()
        for column in (*plan.keys, *plan.rhs_filter_columns)
    }


def _unique_constraints(connection: Any, rhs: exp.Table) -> tuple[tuple[str, ...], ...]:
    schema_name = _table_schema(rhs)
    if schema_name:
        query = (
            "select constraint_column_names "
            "from duckdb_constraints() "
            "where table_name = ? "
            "and schema_name = ? "
            "and constraint_type in ('PRIMARY KEY', 'UNIQUE') "
            "order by constraint_index"
        )
        params = (rhs.name, schema_name)
    else:
        query = (
            "select constraint_column_names "
            "from duckdb_constraints() "
            "where table_name = ? "
            "and constraint_type in ('PRIMARY KEY', 'UNIQUE') "
            "order by constraint_index"
        )
        params = (rhs.name,)

    try:
        rows = connection.execute(query, params).fetchall()
    except Exception:
        return ()

    return tuple(tuple(row[0]) for row in rows)


def _table_schema(table: exp.Table) -> str:
    db = table.args.get("db")
    if isinstance(db, exp.Identifier):
        return db.name
    if isinstance(db, str):
        return db
    return ""


def _format_columns(columns: tuple[str, ...]) -> str:
    if len(columns) == 1:
        return f"column {columns[0]}"
    return f"columns {', '.join(columns)}"
