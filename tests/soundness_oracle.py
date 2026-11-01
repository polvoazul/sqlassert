"""Soundness oracle: judge PROVED assertions against DuckDB on tiny tables.

For each PROVED assertion, Hypothesis generates table contents that satisfy
the program's DDL, DuckDB runs the program unchanged, and the asserted
Property is checked on the real result. A violation is a soundness bug in the
rules; Hypothesis shrinks it to a minimal instance. For select-project-join
queries two rows per table suffice for any uniqueness counterexample (Paulley
2000, Thm 11 and 12); beyond that fragment this is a random search. See
docs/issues/0027-soundness-oracle-against-duckdb.md.

Run it over every program the test suite analyzes::

    python -m pytest --soundness-oracle

With the option, `conftest.py` records every `analyze()` call the suite makes,
and the marked oracle test runs last to judge them. Without it the oracle test
is skipped.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

import duckdb
import sqlglot
from hypothesis import HealthCheck, assume, find, settings
from hypothesis import strategies as st
from hypothesis.errors import NoSuchExample, Unsatisfiable
from sqlglot import exp

import sqlassert
import sqlassert.main
from sqlassert import ExactlyOneJoin, NonNullColumns, NonNullUniqueSet, Outcome, Report, UniqueJoin, UniqueSet
from sqlassert.properties import EnumeratedDomain, ForeignKey, FunctionalDependency
from sqlassert.ir.convert import IrParser
from sqlassert.properties import Property
from sqlassert.provenance import SQL, Origin
from sqlassert.sql_parse import (
    DECLARED_KINDS, ColumnMarker, ParsedProgram, SqlParser, assertion_line, column_markers, join_origin,
)

# Rows per table. Two complete the search for select-project-join queries; one
# more gives aggregates and set operations a little room.
MAX_ROWS = 3
MAX_EXAMPLES = 200

# Counterexamples already recorded as findings in issue 27, keyed by
# (test id, assertion id). They are reported, but do not fail the oracle.
KNOWN_UNSOUND: dict[tuple[str, str], str] = {}

# The marker kind each asserted Property is written with.
_MARKER_KINDS: dict[type[Property], str] = {
    UniqueSet: "unique", NonNullUniqueSet: "key", NonNullColumns: "not-null", ForeignKey: "foreign-key",
    FunctionalDependency: "determines", EnumeratedDomain: "enum",
}


# Recording the suite's analyses -------------------------------------------


@dataclass(frozen=True)
class RecordedProgram:
    sql: str
    dialect: str
    report: Report
    test_id: str


_recorded: dict[tuple[str, str], RecordedProgram] = {}


def record_analyze_calls() -> None:
    """Wrap `analyze` so every call made by a test is kept for the oracle.

    Test modules import `analyze` from `sqlassert`, so this must run before
    they are collected.
    """
    original = sqlassert.main.analyze

    def recording(sql: str, *, dialect: str = sqlassert.main.DEFAULT_DIALECT) -> Report:
        report = original(sql, dialect=dialect)
        test_id = os.environ.get("PYTEST_CURRENT_TEST", "?").rsplit(" ", 1)[0]
        _recorded.setdefault((sql, dialect), RecordedProgram(sql, dialect, report, test_id))
        return report

    setattr(sqlassert, "analyze", recording)
    setattr(sqlassert.main, "analyze", recording)


def recorded_programs() -> tuple[RecordedProgram, ...]:
    return tuple(_recorded.values())


# Judging ----------------------------------------------------------------


Instance = dict[str, list[tuple]]


@dataclass(frozen=True)
class Counterexample:
    assertion_id: str
    origin: Origin
    check: str
    instance: Instance
    witness: list[tuple]

    def explain(self) -> str:
        tables = "\n".join(f"  {name}: {rows}" for name, rows in self.instance.items())
        return (
            f"{self.assertion_id} on line {self.origin.line}: {self.origin.detail}\n"
            f"instance:\n{tables}\n"
            f"violation query:\n  {self.check}\n"
            f"returned: {self.witness}"
        )


@dataclass(frozen=True)
class Judgement:
    checked: int = 0
    counterexamples: tuple[Counterexample, ...] = ()
    skipped: tuple[str, ...] = ()


def judge(sql: str, report: Report, *, dialect: str = "duckdb", max_examples: int = MAX_EXAMPLES) -> Judgement:
    """Search for a counterexample to every PROVED assertion in `report`."""
    proved = [index for index, assertion in enumerate(report.assertions) if assertion.outcome is Outcome.PROVED]
    if not proved:
        return Judgement()

    parsed = SqlParser(dialect).parse(sql)
    properties = [assertion.property for assertion in IrParser(dialect).parse(parsed).program.assertions]
    assert len(properties) == len(report.assertions), "re-parsing the program produced different assertions"

    try:
        database = _Database(parsed)
        declarations = [_violation_query(select, marker) for select, marker in _declared(parsed)]
        for query in declarations:
            database.connection.execute(query)
    except duckdb.Error as error:
        return Judgement(skipped=(f"program cannot run on DuckDB: {_first_line(error)}",))

    checked = 0
    counterexamples: list[Counterexample] = []
    skipped: list[str] = []
    for index in proved:
        assertion = report.assertions[index]
        check = _check(parsed, properties[index], assertion.origin, dialect)
        try:
            database.connection.execute(check)
        except duckdb.Error as error:
            skipped.append(f"{assertion.assertion_id} cannot run on DuckDB: {_first_line(error)}")
            continue
        checked += 1
        instance = database.search(lambda rows: database.violated(rows, declarations, check), max_examples)
        if instance is not None:
            database.load(instance)
            witness = database.connection.execute(check).fetchall()
            counterexamples.append(Counterexample(assertion.assertion_id, assertion.origin, check, instance, witness))
    return Judgement(checked, tuple(counterexamples), tuple(skipped))


def _first_line(error: Exception) -> str:
    return str(error).splitlines()[0]


# Locating and checking each assertion -----------------------------------


def _check(parsed: ParsedProgram, property: Property, origin: Origin, dialect: str) -> str:
    """A query that returns rows exactly when `property` is violated."""
    statements = [statement for statement in (*parsed.create_statements, parsed.root_select) if statement is not None]
    match property:
        case UniqueJoin(join=_) | ExactlyOneJoin(join=_):
            join, = (
                join for statement in statements for join in statement.find_all(exp.Join)
                if assertion_line(join) is not None and join_origin(join, dialect) == origin
            )
            return _join_violation(join, exactly_one=isinstance(property, ExactlyOneJoin))
        case (
            ForeignKey(columns=asserted) | UniqueSet(columns=asserted) | NonNullColumns(columns=asserted)
            | FunctionalDependency(determinant=asserted)
        ):
            kind = _MARKER_KINDS[type(property)]
            columns = {column.name.lower() for column in asserted}
            values: set[str] | None = None
        case EnumeratedDomain(column=column, values=domain):
            kind = _MARKER_KINDS[type(property)]
            columns = {column.name.lower()}
            values = {value.text for value in domain}
        case _:
            raise TypeError(f"Unhandled asserted Property: {type(property).__name__}")
    select, marker = next(
        (select, marker)
        for statement in statements
        for select in statement.find_all(exp.Select)
        for marker in column_markers(select)
        if marker.kind == kind and {name.lower() for name in marker.columns} == columns
        and (values is None or {value.this for value in marker.values} == values)
        and Origin(SQL, select.sql(dialect=dialect), marker.line) == origin
    )
    return _violation_query(select, marker)


def _declared(parsed: ParsedProgram) -> list[tuple[exp.Select, ColumnMarker]]:
    """Every Property Declaration, as the Select it is written on."""
    statements = [statement for statement in (*parsed.create_statements, parsed.root_select) if statement is not None]
    return [
        # A declared Foreign Key is Referential Coverage only: its target's
        # uniqueness is still proved, so instances need not respect it.
        (select, marker._replace(kind="coverage" if marker.kind == "declared-foreign-key" else marker.kind.removeprefix("declared-")))
        for statement in statements
        for select in statement.find_all(exp.Select)
        for marker in column_markers(select)
        if marker.kind in DECLARED_KINDS
    ]


def _violation_query(select: exp.Select, marker: ColumnMarker) -> str:
    kind = marker.kind
    columns = [_quoted(name) for name in marker.columns]
    any_null = " OR ".join(f"{column} IS NULL" for column in columns)
    all_present = " AND ".join(f"{column} IS NOT NULL" for column in columns)
    relation = f"({select.sql(dialect='duckdb')}) AS q"
    duplicates = f"SELECT {', '.join(columns)}, count(*) FROM {relation} WHERE {all_present} GROUP BY ALL HAVING count(*) > 1"
    nulls = f"SELECT {', '.join(columns)} FROM {relation} WHERE {any_null}"
    match kind:
        case "unique":
            query = duplicates
        case "key":
            query = f"{duplicates} UNION ALL SELECT *, NULL FROM ({nulls})"
        case "not-null":
            query = nulls
        case "determines":
            # Rows agreeing on a non-null determinant with more than one
            # dependent value; DISTINCT counts NULL as one value.
            dependent = ", ".join(_quoted(name) for name in marker.dependent)
            values = f"SELECT DISTINCT {', '.join(columns)}, {dependent} FROM {relation} WHERE {all_present}"
            query = f"SELECT {', '.join(columns)}, count(*) FROM ({values}) GROUP BY ALL HAVING count(*) > 1"
        case "enum":
            # NULL is outside the domain and never violates it.
            listed = ", ".join(value.sql(dialect="duckdb") for value in marker.values)
            query = f"SELECT {columns[0]} FROM {relation} WHERE {all_present} AND {columns[0]} NOT IN ({listed})"
        case "foreign-key" | "coverage":
            # MATCH SIMPLE: a row with a null referencing column is exempt.
            assert marker.referenced_relation is not None
            referenced = [_quoted(name) for name in marker.referenced_columns]
            target = marker.referenced_relation
            matches = " AND ".join(f"r.{key} = q.{column}" for key, column in zip(referenced, columns))
            unmatched = f"SELECT {', '.join(columns)} FROM {relation} WHERE {all_present} AND NOT EXISTS (SELECT 1 FROM {target} AS r WHERE {matches})"
            query = unmatched
            if kind == "foreign-key":
                # The referenced columns must also form a Unique Set.
                present = " AND ".join(f"{key} IS NOT NULL" for key in referenced)
                repeated = f"SELECT {', '.join(referenced)} FROM {target} WHERE {present} GROUP BY ALL HAVING count(*) > 1"
                query = f"{unmatched} UNION ALL {repeated}"
        case _:
            raise ValueError(f"Unhandled column marker kind: {kind}")
    return _with_context(select, query)


def _quoted(name: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect="duckdb")


def _join_violation(join: exp.Join, *, exactly_one: bool) -> str:
    """Rows of the join's left input against rows of the join made LEFT.

    As a LEFT join every left row contributes max(1, matches) rows, so the
    counts differ exactly when some left row matches more than once. For
    exactly one match, the right input also carries a flag that is null
    exactly on the row a left row with no match is null-extended with.
    """
    select = join.parent
    assert isinstance(select, exp.Select), "an asserted join is one of a Select's joins"
    joins = select.args["joins"]
    position = next(index for index, candidate in enumerate(joins) if candidate is join)
    source = (select.args.get("from_") or select.args["from"]).this

    def from_joins(chosen: list[exp.Join]) -> str:
        query = exp.select("*").from_(source.copy())
        query.set("joins", chosen)
        return query.sql(dialect="duckdb")

    as_left = join.copy()
    as_left.set("side", "LEFT")
    as_left.set("kind", None)
    if not (as_left.args.get("on") or as_left.args.get("using") or as_left.args.get("method")):
        as_left.set("on", exp.true())
    if exactly_one:
        # The flagged right input keeps its name, so the predicate still reads its columns.
        right = as_left.this
        flagged = exp.select("*", f"1 AS {_MATCHED}").from_(right.copy()).subquery(exp.to_identifier(right.alias_or_name))
        as_left.set("this", flagged)
    left = from_joins([earlier.copy() for earlier in joins[:position]])
    joined = from_joins([*(earlier.copy() for earlier in joins[:position]), as_left])
    counts = f"(SELECT count(*) FROM ({left})) AS left_rows, (SELECT count(*) FROM ({joined})) AS joined_rows"
    violated = "joined_rows > left_rows"
    if exactly_one:
        counts += f", (SELECT count(*) FROM ({joined}) WHERE {_MATCHED} IS NULL) AS unmatched_rows"
        violated += " OR unmatched_rows > 0"
    query = f"SELECT * FROM (SELECT {counts}) WHERE {violated}"
    # From the join, so the Select's own WITH is in scope: the queries above
    # are new Selects that do not carry it.
    return _with_context(join, query)


# The flag column an exactly-one check adds to the right input of its join.
_MATCHED = '"__sqlassert_matched"'


def _with_context(node: exp.Expression, query: str) -> str:
    """`query` prefixed with every CTE in scope at `node` from enclosing queries.

    A CTE whose own body holds `node` brings only the CTEs defined before it.
    """
    ctes: list[exp.Expression] = []
    recursive = False
    child, parent = node, node.parent
    while parent is not None:
        if isinstance(parent, exp.With):
            position = next(index for index, cte in enumerate(parent.expressions) if cte is child)
            ctes = [*parent.expressions[:position], *ctes]
            recursive |= bool(parent.args.get("recursive"))
        elif isinstance(parent, exp.Query):
            with_ = parent.args.get("with_") or parent.args.get("with")
            if isinstance(with_, exp.With) and with_ is not child:
                ctes = [*with_.expressions, *ctes]
                recursive |= bool(with_.args.get("recursive"))
        child, parent = parent, parent.parent
    if not ctes:
        return query
    prefix = "WITH RECURSIVE " if recursive else "WITH "
    return prefix + ", ".join(cte.sql(dialect="duckdb") for cte in ctes) + " " + query


# Generating instances ---------------------------------------------------


@dataclass(frozen=True)
class _Table:
    name: str
    columns: tuple[str, ...]
    domains: tuple[tuple[object, ...], ...]
    keys: tuple[tuple[int, ...], ...]


class _Database:
    """One in-memory DuckDB holding the program's DDL, refilled per example."""

    def __init__(self, parsed: ParsedProgram) -> None:
        self.connection = duckdb.connect()
        self._create(parsed.create_statements)
        numbers, strings = _literals(parsed)
        self.tables = tuple(self._tables(numbers, strings))

    def _create(self, statements: tuple[exp.Expression, ...]) -> None:
        """Run the DDL. A program may reference a relation declared later, which
        DuckDB rejects, so a failed statement is retried once the others ran."""
        pending = [_runnable_create(statement) for statement in statements]
        while pending:
            failed: list[tuple[str, duckdb.Error]] = []
            for statement in pending:
                try:
                    self.connection.execute(statement)
                except duckdb.CatalogException as error:
                    failed.append((statement, error))
            if len(failed) == len(pending):
                raise failed[0][1]
            pending = [statement for statement, _ in failed]

    def _tables(self, numbers: list[object], strings: list[str]) -> list[_Table]:
        rows = self.connection.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() WHERE NOT internal ORDER BY schema_name, table_name"
        ).fetchall()
        tables = []
        for schema, name in rows:
            columns = self.connection.execute(
                "SELECT column_name, data_type, is_nullable FROM duckdb_columns() "
                "WHERE schema_name = ? AND table_name = ? ORDER BY column_index",
                [schema, name],
            ).fetchall()
            names = tuple(column for column, _, _ in columns)
            keys = self.connection.execute(
                "SELECT constraint_column_names FROM duckdb_constraints() "
                "WHERE schema_name = ? AND table_name = ? AND constraint_type IN ('PRIMARY KEY', 'UNIQUE')",
                [schema, name],
            ).fetchall()
            tables.append(_Table(
                name=name if schema == "main" else f"{schema}.{name}",
                columns=names,
                domains=tuple(
                    _domain(data_type, numbers, strings) + ((None,) if nullable else ())
                    for _, data_type, nullable in columns
                ),
                keys=tuple(tuple(names.index(column) for column in key) for key, in keys),
            ))
        return tables

    def search(self, violated: Callable[[Instance], bool], max_examples: int) -> Instance | None:
        """The smallest instance Hypothesis finds that satisfies `violated`."""
        strategy = st.fixed_dictionaries({
            table.name: st.lists(st.tuples(*(st.sampled_from(domain) for domain in table.domains)), max_size=MAX_ROWS)
            .map(lambda rows, table=table: _respecting_keys(rows, table.keys))
            for table in self.tables
        })
        try:
            return find(strategy, violated, settings=settings(
                max_examples=max_examples, database=None, deadline=None, suppress_health_check=list(HealthCheck),
            ))
        except (NoSuchExample, Unsatisfiable):
            return None

    def violated(self, instance: Instance, declarations: list[str], check: str) -> bool:
        try:
            self.load(instance)
            for declaration in declarations:
                assume(not self.connection.execute(declaration).fetchall())
            return bool(self.connection.execute(check).fetchall())
        except duckdb.Error:
            # A constraint the generator does not model, such as CHECK, or a
            # value the query cannot cast: the program rejects this data.
            assume(False)
            raise

    def load(self, instance: Instance) -> None:
        for table in self.tables:
            self.connection.execute(f"DELETE FROM {table.name}")
            rows = instance[table.name]
            if rows:
                placeholders = ", ".join("?" for _ in table.columns)
                self.connection.executemany(f"INSERT INTO {table.name} VALUES ({placeholders})", rows)


def _runnable_create(statement: exp.Expression) -> str:
    """The statement as DuckDB runs it. A table created from a query becomes a
    view, so its contents follow from generated data instead of being generated."""
    if isinstance(statement, exp.Create) and (statement.kind or "").upper() == "TABLE" and statement.expression:
        statement = statement.copy()
        statement.set("kind", "VIEW")
    return statement.sql(dialect="duckdb")


def _respecting_keys(rows: list[tuple], keys: tuple[tuple[int, ...], ...]) -> list[tuple]:
    """Drop each row that repeats an earlier row's complete key."""
    seen: set[tuple] = set()
    kept = []
    for row in rows:
        values = [(key, tuple(row[index] for index in key)) for key in keys]
        complete = [value for value in values if None not in value[1]]
        if any(value in seen for value in complete):
            continue
        seen.update(complete)
        kept.append(row)
    return kept


def _literals(parsed: ParsedProgram) -> tuple[list[object], list[str]]:
    """The program's own literals, so its filters and join constants are hit,
    and its ENUM markers' values, so instances can respect a declared domain."""
    numbers: list[object] = []
    strings: list[str] = []
    for statement in (*parsed.create_statements, parsed.root_select):
        if statement is None:
            continue
        marked = (value for select in statement.find_all(exp.Select) for marker in column_markers(select) for value in marker.values)
        for literal in (*statement.find_all(exp.Literal), *marked):
            assert isinstance(literal, exp.Literal)
            if literal.is_string:
                strings.append(literal.this)
            else:
                number = float(literal.this)
                numbers.append(int(number) if number.is_integer() else number)
    return numbers, strings


def _domain(data_type: str, numbers: list[object], strings: list[str]) -> tuple[object, ...]:
    """A tiny value domain for one column, so collisions are frequent."""
    family = sqlglot.exp.DataType.build(data_type, dialect="duckdb")
    if family.is_type(*exp.DataType.NUMERIC_TYPES):
        values: list[object] = [1, 2, *numbers]
    elif family.is_type(*exp.DataType.TEXT_TYPES):
        values = ["1", "2", *strings]
    elif family.is_type(exp.DataType.Type.BOOLEAN):
        values = [True, False]
    elif family.is_type(*exp.DataType.TEMPORAL_TYPES):
        values = ["2020-01-01", "2020-01-02"]
    else:
        values = ["1", "2", *strings]
    return tuple(dict.fromkeys(values))
