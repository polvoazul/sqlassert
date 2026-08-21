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
    marker_count = len(MARKER.findall(sql))
    if not marker_count:
        return []

    try:
        expressions = [expression for expression in sqlglot.parse(_attach_markers_to_joins(sql, dialect), read=dialect) if expression]
    except SqlglotError:
        return [_UniqueJoinPlan(index, "SQL parse failed") for index in range(marker_count)]

    joins = [
        join
        for expression in expressions
        for join in expression.find_all(exp.Join)
        for comment in join.comments or []
        if _is_unique_marker(comment)
    ]
    plans = [_plan_for_join(index, join, dialect) for index, join in enumerate(joins)]
    plans.extend(_UniqueJoinPlan(index, "marker is not followed by a join") for index in range(len(plans), marker_count))
    return plans

def _is_unique_marker(comment: str) -> bool:
    return comment.strip("* ").lower() == "unique"


def _attach_markers_to_joins(sql: str, dialect: str) -> str:
    markers = list(MARKER.finditer(sql))
    join_tokens = [token for token in Tokenizer(dialect=dialect).tokenize(sql) if token.token_type is TokenType.JOIN]
    replacements: list[tuple[int, int, str]] = []

    for marker in markers:
        join = next((token for token in join_tokens if token.start >= marker.end()), None)
        if join is None:
            continue
        # SQLGlot attaches a comment before JOIN to the preceding expression.
        # A comment after JOIN is attached to the Join AST node.
        replacements.append((marker.start(), marker.end(), ""))
        replacements.append((join.end + 1, join.end + 1, " /**UNIQUE**/"))

    for start, end, replacement in sorted(replacements, reverse=True):
        sql = f"{sql[:start]}{replacement}{sql[end:]}"
    return sql


def _plan_for_join(marker_index: int, join: exp.Join, dialect: str) -> _UniqueJoinPlan:
    join_sql = _join_sql(join, dialect)
    kind = (join.args.get("kind") or "").upper()
    if kind in {"ANTI", "SEMI"}:
        return _UniqueJoinPlan(
            marker_index,
            f'{_join_reason_prefix(join_sql)}, {kind.lower()} joins are not supported',
            join_sql,
        )

    rhs = join.this
    on = join.args.get("on")
    using = join.args.get("using") or []
    if rhs is None:
        return _UniqueJoinPlan(marker_index, f"{_join_reason_prefix(join_sql)}, marked join has no RHS relation", join_sql)
    if on is None and not using:
        return _UniqueJoinPlan(marker_index, f"{_join_reason_prefix(join_sql)}, marked join has no ON or USING predicate", join_sql)

    rhs_names = _rhs_names(rhs)
    if not rhs_names:
        return _UniqueJoinPlan(
            marker_index,
            f"{_join_reason_prefix(join_sql)}, could not identify RHS relation name or alias",
            join_sql,
        )

    keys = _rhs_using_columns(using) if using else _rhs_key_columns(on, rhs_names)
    if not keys:
        return _UniqueJoinPlan(
            marker_index,
            f"{_join_reason_prefix(join_sql)}, could not infer RHS key columns from join predicate",
            join_sql,
        )

    return _UniqueJoinPlan(
        marker_index=marker_index,
        reason="join predicate inferred",
        join_sql=join_sql,
        rhs=rhs,
        rhs_names=frozenset(rhs_names),
        keys=tuple(keys),
        rhs_filter_columns=tuple(_rhs_filter_columns(on, rhs_names) if on is not None else ()),
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

    unique_constraints = _relation_unique_constraints(connection, plan.rhs)
    if not unique_constraints:
        return UniqueJoinCheckResult(
            marker_index=plan.marker_index,
            valid=False,
            reason=_cannot_prove_reason(plan),
            inferred_key_columns=key_names,
            unique_constraints=unique_constraints,
        )

    constraint_check = _validate_constraints(plan, key_names, unique_constraints)
    if constraint_check is not None:
        return constraint_check

    return UniqueJoinCheckResult(
        marker_index=plan.marker_index,
        valid=False,
        reason=_cannot_prove_reason(plan),
        inferred_key_columns=key_names,
        unique_constraints=unique_constraints,
    )


def _validate_constraints(
    plan: _UniqueJoinPlan,
    key_names: tuple[str, ...],
    unique_constraints: tuple[tuple[str, ...], ...],
) -> UniqueJoinCheckResult | None:
    covered_columns = _covered_rhs_column_names(plan)
    for constraint in unique_constraints:
        if all(column.lower() in covered_columns for column in constraint):
            constrained_key_columns = tuple(constraint)
            return UniqueJoinCheckResult(
                marker_index=plan.marker_index,
                valid=True,
                reason=(
                    f"RHS uniqueness proof ({', '.join(constrained_key_columns)}) "
                    "is covered by inferred keys/filters "
                    f"({', '.join(sorted(covered_columns))})"
                ),
                inferred_key_columns=key_names,
                constrained_key_columns=constrained_key_columns,
                unique_constraints=unique_constraints,
            )
    return None


def _cannot_prove_reason(plan: _UniqueJoinPlan) -> str:
    rhs_columns = _format_columns(tuple(key.name for key in plan.keys))
    verb = "is" if len(plan.keys) == 1 else "are"
    return f"{_join_reason_prefix(plan.join_sql)}, we can't prove that RHS {rhs_columns} {verb} unique"


def _join_reason_prefix(join_sql: str) -> str:
    return f'in join: "{join_sql}"'


def _join_sql(join: exp.Join, dialect: str) -> str:
    return " ".join(join.sql(dialect=dialect, comments=False).split())


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


def _rhs_using_columns(using: list[exp.Identifier]) -> list[exp.Column]:
    keys = []
    seen = set()
    for identifier in using:
        name = identifier.name
        if name.lower() in seen:
            continue
        keys.append(exp.column(name))
        seen.add(name.lower())
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


def _relation_unique_constraints(
    connection: Any,
    relation: exp.Expression,
    seen_views: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[tuple[str, ...], ...]:
    if isinstance(relation, exp.Table):
        constraints = _unique_constraints(connection, relation)
        if constraints:
            return constraints
        return _view_unique_constraints(connection, relation, seen_views)

    if isinstance(relation, exp.Subquery) and isinstance(relation.this, exp.Select):
        return _select_unique_constraints(connection, relation.this, seen_views)

    return ()


def _view_unique_constraints(
    connection: Any,
    view: exp.Table,
    seen_views: frozenset[tuple[str, str]],
) -> tuple[tuple[str, ...], ...]:
    schema_name = _table_schema(view) or "main"
    view_key = (schema_name.lower(), view.name.lower())
    if view_key in seen_views:
        return ()

    view_sql = _view_sql(connection, view)
    if not view_sql:
        return ()

    try:
        expression = sqlglot.parse_one(view_sql, read="duckdb")
    except SqlglotError:
        return ()

    if not isinstance(expression, exp.Create) or not isinstance(expression.expression, exp.Select):
        return ()

    return _select_unique_constraints(connection, expression.expression, seen_views | {view_key})


def _view_sql(connection: Any, view: exp.Table) -> str:
    schema_name = _table_schema(view)
    if schema_name:
        query = (
            "select sql "
            "from duckdb_views() "
            "where view_name = ? "
            "and schema_name = ? "
            "and not internal "
            "order by database_name, schema_name "
            "limit 1"
        )
        params = (view.name, schema_name)
    else:
        query = (
            "select sql "
            "from duckdb_views() "
            "where view_name = ? "
            "and not internal "
            "order by database_name, schema_name "
            "limit 1"
        )
        params = (view.name,)

    try:
        row = connection.execute(query, params).fetchone()
    except Exception:
        return ""

    return row[0] if row else ""


def _select_unique_constraints(
    connection: Any,
    select: exp.Select,
    seen_views: frozenset[tuple[str, str]],
) -> tuple[tuple[str, ...], ...]:
    constraints = []
    group_constraint = _group_by_constraint(select)
    if group_constraint:
        constraints.append(group_constraint)

    distinct_constraint = _distinct_constraint(select)
    if distinct_constraint:
        constraints.append(distinct_constraint)

    qualify_constraint = _qualify_row_number_constraint(select)
    if qualify_constraint:
        constraints.append(qualify_constraint)

    constraints.extend(_projected_source_constraints(connection, select, seen_views))

    return _dedupe_constraints(tuple(constraints))


def _projected_source_constraints(
    connection: Any,
    select: exp.Select,
    seen_views: frozenset[tuple[str, str]],
) -> tuple[tuple[str, ...], ...]:
    source = _single_select_source(select)
    if source is None:
        return ()

    source_constraints = _relation_unique_constraints(connection, source, seen_views)
    if not source_constraints:
        return ()

    projection = _projection_map(select.expressions)
    if not projection:
        return ()

    constraints = []
    for constraint in source_constraints:
        mapped = []
        for column in constraint:
            output_name = projection.get(column.lower())
            if output_name is None:
                break
            mapped.append(output_name)
        else:
            constraints.append(tuple(mapped))

    return tuple(constraints)


def _single_select_source(select: exp.Select) -> exp.Expression | None:
    if select.args.get("joins"):
        return None

    from_ = select.args.get("from_")
    if not isinstance(from_, exp.From):
        return None

    source = from_.this
    if isinstance(source, exp.Table | exp.Subquery):
        return source
    return None


def _projection_map(expressions: list[exp.Expression]) -> dict[str, str]:
    projection: dict[str, str] = {}
    for expression in expressions:
        source_name, output_name = _projection_column_names(expression)
        if source_name and output_name and source_name.lower() not in projection:
            projection[source_name.lower()] = output_name
    return projection


def _projection_column_names(expression: exp.Expression) -> tuple[str, str]:
    if isinstance(expression, exp.Alias) and isinstance(expression.this, exp.Column):
        return expression.this.name, expression.alias
    if isinstance(expression, exp.Column):
        return expression.name, expression.name
    return "", ""


def _dedupe_constraints(constraints: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
    deduped = []
    seen = set()
    for constraint in constraints:
        key = tuple(column.lower() for column in constraint)
        if key in seen:
            continue
        deduped.append(constraint)
        seen.add(key)
    return tuple(deduped)


def _group_by_constraint(select: exp.Select) -> tuple[str, ...]:
    group = select.args.get("group")
    if not isinstance(group, exp.Group):
        return ()
    return _simple_output_columns(group.expressions)


def _distinct_constraint(select: exp.Select) -> tuple[str, ...]:
    if select.args.get("distinct") is None:
        return ()
    return _simple_output_columns(select.expressions)


def _qualify_row_number_constraint(select: exp.Select) -> tuple[str, ...]:
    qualify = select.args.get("qualify")
    if not isinstance(qualify, exp.Qualify):
        return ()

    window = _row_number_window_filtered_to_one(qualify.this)
    if window is None:
        return ()
    return _simple_output_columns(window.args.get("partition_by") or [])


def _row_number_window_filtered_to_one(expression: exp.Expression) -> exp.Window | None:
    if not isinstance(expression, exp.EQ):
        return None

    if _is_row_number_window(expression.this) and _is_one(expression.expression):
        return expression.this
    if _is_row_number_window(expression.expression) and _is_one(expression.this):
        return expression.expression
    return None


def _is_row_number_window(expression: exp.Expression) -> bool:
    return isinstance(expression, exp.Window) and isinstance(expression.this, exp.RowNumber)


def _is_one(expression: exp.Expression) -> bool:
    return isinstance(expression, exp.Literal) and not expression.is_string and expression.this == "1"


def _simple_output_columns(expressions: Iterable[exp.Expression]) -> tuple[str, ...]:
    columns = []
    seen = set()
    for expression in expressions:
        name = _simple_output_column_name(expression)
        if name and name.lower() not in seen:
            columns.append(name)
            seen.add(name.lower())
    return tuple(columns)


def _simple_output_column_name(expression: exp.Expression) -> str:
    if isinstance(expression, exp.Alias):
        return expression.alias
    if isinstance(expression, exp.Column):
        return expression.name
    return ""


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
