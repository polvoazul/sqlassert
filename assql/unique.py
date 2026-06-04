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
MARKER = "/**unique**/"
RHS_ALIAS = "__assql_rhs"


@dataclass(frozen=True)
class UniqueJoinCheckResult:
    marker_index: int
    valid: bool
    reason: str
    assertion_sql: str | None = None
    duplicates_sql: str | None = None
    inferred_key_columns: tuple[str, ...] = ()
    primary_key_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class UniqueJoinValidationResult:
    valid: bool
    reason: str
    checks: tuple[UniqueJoinCheckResult, ...] = ()


@dataclass(frozen=True)
class _AssertionPlan:
    marker_index: int
    assertion_sql: str | None
    duplicates_sql: str | None
    reason: str
    rhs: exp.Expression | None = None
    rhs_names: frozenset[str] = frozenset()
    keys: tuple[exp.Column, ...] = ()
    rhs_filter_columns: tuple[exp.Column, ...] = ()


def validate_unique_joins(
    connection: Any,
    sql: str,
    dialect: str = "duckdb",
) -> UniqueJoinValidationResult:
    plans = _assertion_plans(sql, dialect)
    if not plans:
        return UniqueJoinValidationResult(True, "no unique join markers found")

    checks = tuple(_validate_plan(connection, plan, dialect) for plan in plans)
    if all(check.valid for check in checks):
        return UniqueJoinValidationResult(True, "all unique join assertions passed", checks)

    reason = "; ".join(check.reason for check in checks if not check.valid)
    return UniqueJoinValidationResult(False, reason, checks)


def unique_assertions(sql: str, dialect: str = "duckdb") -> list[str]:
    return [
        plan.assertion_sql if plan.assertion_sql is not None else FALSE_ASSERTION
        for plan in _assertion_plans(sql, dialect)
    ]


def _assertion_plans(sql: str, dialect: str) -> list[_AssertionPlan]:
    marker_join_indexes = _marked_join_indexes(sql, dialect)
    if not marker_join_indexes:
        return []

    try:
        expressions = [expression for expression in sqlglot.parse(sql, read=dialect) if expression]
    except SqlglotError:
        return [
            _AssertionPlan(index, None, None, "SQL parse failed")
            for index, _ in enumerate(marker_join_indexes)
        ]

    joins: list[exp.Join] = []
    for expression in expressions:
        joins.extend(expression.find_all(exp.Join))

    plans: list[_AssertionPlan] = []
    for marker_index, join_index in enumerate(marker_join_indexes):
        if join_index is None or join_index >= len(joins):
            plans.append(_AssertionPlan(marker_index, None, None, "marker is not followed by a join"))
            continue
        plans.append(_assertion_plan_for_join(marker_index, joins[join_index], dialect))
    return plans


def _marked_join_indexes(sql: str, dialect: str) -> list[int | None]:
    marker_offsets = [match.end() for match in re.finditer(re.escape(MARKER), sql)]
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


def _assertion_plan_for_join(marker_index: int, join: exp.Join, dialect: str) -> _AssertionPlan:
    kind = (join.args.get("kind") or "").upper()
    if kind in {"ANTI", "SEMI"}:
        return _AssertionPlan(marker_index, None, None, f"{kind.lower()} joins are not supported")

    rhs = join.this
    on = join.args.get("on")
    if rhs is None or on is None:
        return _AssertionPlan(marker_index, None, None, "marked join has no ON predicate")

    rhs_names = _rhs_names(rhs)
    if not rhs_names:
        return _AssertionPlan(marker_index, None, None, "could not identify RHS relation name or alias")

    keys = _rhs_key_columns(on, rhs_names)
    if not keys:
        return _AssertionPlan(marker_index, None, None, "could not infer RHS key columns")

    source_sql = _rhs_source_sql(rhs, dialect)
    if not source_sql:
        return _AssertionPlan(marker_index, None, None, "could not render RHS SQL")

    select_keys = []
    for index, key in enumerate(keys):
        key_sql = _rewrite_rhs_columns(key, rhs_names).sql(dialect=dialect)
        select_keys.append(f"{key_sql} as __assql_key_{index}")

    group_by = ", ".join(str(index) for index in range(1, len(keys) + 1))
    where_sql = _where_sql(on, rhs_names, dialect)
    where_clause = f"\n  where {where_sql}" if where_sql else ""

    duplicates_sql = (
        f"  select {', '.join(select_keys)}, count(*) as n\n"
        f"  from {source_sql}"
        f"{where_clause}\n"
        f"  group by {group_by}\n"
        "  having count(*) > 1\n"
        "  limit 1"
    )
    assertion_sql = (
        "select count(*) = 0\n"
        "from (\n"
        f"{duplicates_sql}\n"
        ") as unique_join_duplicates"
    )

    return _AssertionPlan(
        marker_index=marker_index,
        assertion_sql=assertion_sql,
        duplicates_sql=duplicates_sql,
        reason="assertion generated",
        rhs=rhs,
        rhs_names=frozenset(rhs_names),
        keys=tuple(keys),
        rhs_filter_columns=tuple(_rhs_filter_columns(on, rhs_names)),
    )


def _validate_plan(connection: Any, plan: _AssertionPlan, dialect: str) -> UniqueJoinCheckResult:
    key_names = tuple(key.name for key in plan.keys)
    if plan.duplicates_sql is None:
        return UniqueJoinCheckResult(
            marker_index=plan.marker_index,
            valid=False,
            reason=plan.reason,
            inferred_key_columns=key_names,
        )

    primary_key_columns = _primary_key_columns(connection, plan.rhs, dialect)
    if primary_key_columns and _primary_key_is_covered(primary_key_columns, plan):
        return UniqueJoinCheckResult(
            marker_index=plan.marker_index,
            valid=True,
            reason=(
                "RHS primary key "
                f"({', '.join(primary_key_columns)}) is covered by inferred keys/filters"
            ),
            assertion_sql=plan.assertion_sql,
            duplicates_sql=plan.duplicates_sql,
            inferred_key_columns=key_names,
            primary_key_columns=primary_key_columns,
        )

    try:
        duplicate_row = connection.execute(plan.duplicates_sql).fetchone()
    except Exception as exc:  # noqa: BLE001 - validation returns reasons instead of raising.
        return UniqueJoinCheckResult(
            marker_index=plan.marker_index,
            valid=False,
            reason=f"assertion query failed: {exc}",
            assertion_sql=plan.assertion_sql,
            duplicates_sql=plan.duplicates_sql,
            inferred_key_columns=key_names,
            primary_key_columns=primary_key_columns,
        )

    if duplicate_row is None:
        reason = "no duplicate RHS keys found"
        if primary_key_columns:
            reason = (
                "RHS primary key was not fully covered, but no duplicate RHS keys were found"
            )
        return UniqueJoinCheckResult(
            marker_index=plan.marker_index,
            valid=True,
            reason=reason,
            assertion_sql=plan.assertion_sql,
            duplicates_sql=plan.duplicates_sql,
            inferred_key_columns=key_names,
            primary_key_columns=primary_key_columns,
        )

    return UniqueJoinCheckResult(
        marker_index=plan.marker_index,
        valid=False,
        reason=_duplicate_reason(key_names, duplicate_row),
        assertion_sql=plan.assertion_sql,
        duplicates_sql=plan.duplicates_sql,
        inferred_key_columns=key_names,
        primary_key_columns=primary_key_columns,
    )


def _rhs_names(rhs: exp.Expression) -> set[str]:
    names = {rhs.alias_or_name}
    if isinstance(rhs, exp.Table):
        names.add(rhs.name)
    return {name.lower() for name in names if name}


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


def _where_sql(on: exp.Expression, rhs_names: set[str], dialect: str) -> str:
    predicates = []
    for predicate in _and_terms(on):
        columns = list(predicate.find_all(exp.Column))
        if columns and all(_is_rhs_column(column, rhs_names) for column in columns):
            predicates.append(_rewrite_rhs_columns(predicate, rhs_names).sql(dialect=dialect))
    return " and ".join(predicates)


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


def _rewrite_rhs_columns(expression: exp.Expression, rhs_names: set[str]) -> exp.Expression:
    def replace(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column) and _is_rhs_column(node, rhs_names):
            rewritten = node.copy()
            rewritten.set("table", exp.to_identifier(RHS_ALIAS))
            return rewritten
        return node

    return expression.copy().transform(replace)


def _rhs_source_sql(rhs: exp.Expression, dialect: str) -> str:
    if isinstance(rhs, exp.Subquery):
        return f"({rhs.this.sql(dialect=dialect)}) as {RHS_ALIAS}"

    rhs_without_alias = rhs.copy()
    rhs_without_alias.set("alias", None)
    rhs_sql = rhs_without_alias.sql(dialect=dialect)
    if not rhs_sql:
        return ""
    return f"{rhs_sql} as {RHS_ALIAS}"


def _primary_key_columns(
    connection: Any,
    rhs: exp.Expression | None,
    dialect: str,
) -> tuple[str, ...]:
    if not isinstance(rhs, exp.Table):
        return ()

    rhs_without_alias = rhs.copy()
    rhs_without_alias.set("alias", None)
    table_name = rhs_without_alias.sql(dialect=dialect)
    table_name_literal = table_name.replace("'", "''")

    try:
        rows = connection.execute(
            f"select name from pragma_table_info('{table_name_literal}') where pk order by cid"
        ).fetchall()
    except Exception:
        return ()

    return tuple(row[0] for row in rows)


def _primary_key_is_covered(primary_key_columns: tuple[str, ...], plan: _AssertionPlan) -> bool:
    covered_columns = {
        column.name.lower()
        for column in (*plan.keys, *plan.rhs_filter_columns)
    }
    return all(column.lower() in covered_columns for column in primary_key_columns)


def _duplicate_reason(key_names: tuple[str, ...], duplicate_row: tuple[Any, ...]) -> str:
    key_values = duplicate_row[:-1]
    count = duplicate_row[-1]
    pairs = ", ".join(
        f"{name}={value!r}" for name, value in zip(key_names, key_values, strict=False)
    )
    return f"duplicate RHS key found ({pairs}) with {count} rows"
