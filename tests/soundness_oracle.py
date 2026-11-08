"""Soundness oracle: judge PROVED assertions against DuckDB on tiny tables.

For each PROVED assertion, Hypothesis generates table contents that satisfy
the program's DDL, DuckDB runs the program unchanged, and the asserted
Property is checked on the real result. A violation is a soundness bug in the
rules; Hypothesis shrinks it to a minimal instance. Before the random search,
a fixed set of edge-case instances is checked (empty tables, duplicates, rows
differing in one column, NULLs, blank values), so a counterexample of those
shapes is found every time. For select-project-join
queries two rows per table suffice for any uniqueness counterexample (Paulley
2000, Thm 11 and 12); beyond that fragment this is a random search. See
docs/issues/0027-soundness-oracle-against-duckdb.md.

Run it over every program the test suite analyzes::

    python -m pytest --soundness-oracle

With the option, `conftest.py` records each test's `analyze()` calls and judges
them after its fixtures finish. Pytest distributes whole tests across CPUs.

Only programs in the DuckDB dialect are judged; others are skipped with the
reason. Rendering SQL for DuckDB is best-effort and may change its meaning.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, assert_never

import duckdb
import sqlglot
from hypothesis import HealthCheck, assume, find, settings
from hypothesis import strategies as st
from hypothesis.errors import NoSuchExample, Unsatisfiable
from sqlglot import exp

import sqlassert
import sqlassert.main
from sqlassert.diagnostics import UNENFORCED_CONSTRAINT
from sqlassert import (
    ExactlyOneJoin, ManyToOneJoin, NonNullColumns, NonNullUniqueSet, OneToManyJoin, OneToOneJoin, Outcome, Report, UniqueJoin, UniqueSet,
)
from sqlassert.properties import EnumeratedDomain, ExhaustiveCase, ForeignKey, FunctionalDependency, NonNullElements
from sqlassert.ir import LiteralValue
from sqlassert.ir.convert import IrParser
from sqlassert.properties import Property
from sqlassert.provenance import Origin, Span
from sqlassert.sql_parse import (
    DECLARED_KINDS, ColumnMarker, ColumnMarkerKind, ParsedProgram, SqlParser, assertion_span, column_markers, exhaustive_span, join_origin,
    literal_value,
)

# Rows per table. Two complete the search for select-project-join queries; one
# more gives aggregates and set operations a little room.
MAX_ROWS = 3
MAX_EXAMPLES = 200

# Counterexamples already recorded as findings in issue 27, keyed by
# (test id, assertion id). They are reported, but do not fail the oracle.
KNOWN_UNSOUND: dict[tuple[str, str], str] = {}

# The marker kind each asserted Property is written with.
_MARKER_KINDS: dict[type[Property], ColumnMarkerKind] = {
    UniqueSet: "unique", NonNullUniqueSet: "key", NonNullColumns: "not-null", ForeignKey: "foreign-key",
    FunctionalDependency: "determines", EnumeratedDomain: "enum", NonNullElements: "not-null-elements",
}


# Recording the suite's analyses -------------------------------------------


@dataclass(frozen=True)
class RecordedProgram:
    sql: str
    dialect: str
    report: Report


_recorded: dict[tuple[str, str], RecordedProgram] = {}


def record_analyze_calls() -> None:
    """Wrap `analyze` so every call made by a test is kept for the oracle.

    Test modules import `analyze` from `sqlassert`, so this must run before
    they are collected.
    """
    original = sqlassert.main.analyze

    def recording(sql: str, *, dialect: str = sqlassert.main.DEFAULT_DIALECT, trust_unenforced_keys: bool = True) -> Report:
        # Keys trusted by the caller, not the SQL, are not the oracle's to judge:
        # it records the untrusted report, and the trusted one only differs
        # where the untrusted run left a key out.
        report = original(sql, dialect=dialect, trust_unenforced_keys=False)
        _recorded.setdefault((sql, dialect), RecordedProgram(sql, dialect, report))
        if trust_unenforced_keys and any(warning.code == UNENFORCED_CONSTRAINT for warning in report.warnings):
            return original(sql, dialect=dialect, trust_unenforced_keys=True)
        return report

    setattr(sqlassert, "analyze", recording)
    setattr(sqlassert.main, "analyze", recording)


def take_recorded_programs() -> tuple[RecordedProgram, ...]:
    programs = tuple(_recorded.values())
    _recorded.clear()
    return programs


# Judging ----------------------------------------------------------------


Instance = dict[str, list[tuple]]


@dataclass(frozen=True)
class SearchResult:
    """Search outcome and predicate evaluations, including shrinking attempts."""

    status: Literal["counterexample", "no-counterexample", "insufficient-data"]
    instance: Instance | None
    attempted: int
    valid: int

    @property
    def rejected(self) -> int:
        return self.attempted - self.valid


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
    searches: tuple[tuple[str, SearchResult], ...] = ()


def judge(sql: str, report: Report, *, dialect: str = "duckdb", max_examples: int = MAX_EXAMPLES) -> Judgement:
    """Search for a counterexample to every PROVED assertion in `report`."""
    proved = [index for index, assertion in enumerate(report.assertions) if assertion.outcome is Outcome.PROVED]
    if not proved:
        return Judgement()
    if dialect != "duckdb":
        # Rendering for DuckDB is best-effort and may change the query: sqlglot
        # 28 turns Spark's RIGHT SEMI JOIN into a SEMI JOIN. The oracle judges
        # only what DuckDB runs as written.
        return Judgement(skipped=(f"dialect {dialect}: the oracle runs DuckDB SQL only",))

    parsed = SqlParser(dialect).parse(sql)
    properties = [assertion.property for assertion in IrParser(dialect).parse(parsed).program.assertions]
    assert len(properties) == len(report.assertions), "re-parsing the program produced different assertions"

    try:
        database = _Database(parsed)
        declarations = [_violation_query(select, marker, _checked(marker.kind)) for select, marker in _declared(parsed)]
        for query in declarations:
            database.connection.execute(query)
    except duckdb.Error as error:
        return Judgement(skipped=(f"program cannot run on DuckDB: {_first_line(error)}",))

    checked = 0
    counterexamples: list[Counterexample] = []
    skipped: list[str] = []
    searches: list[tuple[str, SearchResult]] = []
    for index in proved:
        assertion = report.assertions[index]
        check = _check(parsed, properties[index], assertion.origin, dialect)
        try:
            database.connection.execute(check)
        except duckdb.Error as error:
            skipped.append(f"{assertion.assertion_id} cannot run on DuckDB: {_first_line(error)}")
            continue
        search = database.search(lambda _: database.violated(declarations, check), max_examples)
        searches.append((assertion.assertion_id, search))
        if search.status == "insufficient-data":
            skipped.append(f"{assertion.assertion_id}: insufficient valid data ({search.valid} valid, {search.rejected} rejected)")
            continue
        checked += 1
        if search.instance is not None:
            database.load(search.instance)
            witness = database.connection.execute(check).fetchall()
            counterexamples.append(Counterexample(assertion.assertion_id, assertion.origin, check, search.instance, witness))
    return Judgement(checked, tuple(counterexamples), tuple(skipped), tuple(searches))


def _first_line(error: Exception) -> str:
    return str(error).splitlines()[0]


# Locating and checking each assertion -----------------------------------


def _check(parsed: ParsedProgram, property: Property, origin: Origin, dialect: str) -> str:
    """A query that returns rows exactly when `property` is violated."""
    statements = [statement for statement in (*parsed.create_statements, parsed.root_select) if statement is not None]
    match property:
        case UniqueJoin(join=_) | ManyToOneJoin(join=_) | OneToManyJoin(join=_) | OneToOneJoin(join=_) | ExactlyOneJoin(join=_):
            join, = (
                join for statement in statements for join in statement.find_all(exp.Join)
                if assertion_span(join) is not None and join_origin(join, dialect) == origin
            )
            return _join_violation(join, property)
        case (
            ForeignKey(columns=asserted) | UniqueSet(columns=asserted) | NonNullColumns(columns=asserted)
            | NonNullElements(columns=asserted) | FunctionalDependency(determinant=asserted)
        ):
            kind = _MARKER_KINDS[type(property)]
            columns = {column.name.lower() for column in asserted}
            values: frozenset[LiteralValue] | None = None
        case EnumeratedDomain(column=column, values=domain):
            kind = _MARKER_KINDS[type(property)]
            columns = {column.name.lower()}
            values = domain
        case ExhaustiveCase(case=_):
            case, = (
                case for statement in statements for case in statement.find_all(exp.Case)
                if (span := exhaustive_span(case)) is not None and Origin.marked(case.sql(dialect=dialect), span) == origin
            )
            return _case_violation(case)
        case _:
            raise TypeError(f"Unhandled asserted Property: {type(property).__name__}")
    select, marker = next(
        (select, marker)
        for statement in statements
        for select in statement.find_all(exp.Select)
        for marker in column_markers(select)
        if marker.kind == kind and {name.lower() for name in marker.columns} == columns
        and (values is None or frozenset(marker.values) == values)
        and Origin.marked(select.sql(dialect=dialect), marker.span) == origin
    )
    return _violation_query(select, marker, _checked(marker.kind))


def _declared(parsed: ParsedProgram) -> list[tuple[exp.Select, ColumnMarker]]:
    """Every Property Declaration, as the Select it is written on."""
    statements = [statement for statement in (*parsed.create_statements, parsed.root_select) if statement is not None]
    return [
        (select, marker)
        for statement in statements
        for select in statement.find_all(exp.Select)
        for marker in column_markers(select)
        if marker.kind in DECLARED_KINDS
    ]


# What a marker's rows must satisfy. A declared Foreign Key is Referential
# Coverage only: its target's uniqueness is still proved, so instances need
# not respect it.
type _Check = Literal["unique", "key", "not-null", "not-null-elements", "determines", "enum", "foreign-key", "coverage"]


def _checked(kind: ColumnMarkerKind) -> _Check:
    match kind:
        case "unique" | "declared-unique":
            return "unique"
        case "key" | "declared-key":
            return "key"
        case "not-null" | "declared-not-null":
            return "not-null"
        case "not-null-elements" | "declared-not-null-elements":
            return "not-null-elements"
        case "determines" | "declared-determines":
            return "determines"
        case "enum" | "declared-enum":
            return "enum"
        case "foreign-key":
            return "foreign-key"
        case "declared-foreign-key":
            return "coverage"
        case _:
            assert_never(kind)


def _violation_query(select: exp.Select, marker: ColumnMarker, kind: _Check) -> str:
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
        case "not-null-elements":
            # A DuckDB list's `list_count` counts its non-null elements.
            holes = " OR ".join(f"list_count({column}) < len({column})" for column in columns)
            query = f"SELECT {', '.join(columns)} FROM {relation} WHERE {holes}"
        case "determines":
            # Rows agreeing on a non-null determinant with more than one
            # dependent value; DISTINCT counts NULL as one value.
            dependent = ", ".join(_quoted(name) for name in marker.dependent)
            values = f"SELECT DISTINCT {', '.join(columns)}, {dependent} FROM {relation} WHERE {all_present}"
            query = f"SELECT {', '.join(columns)}, count(*) FROM ({values}) GROUP BY ALL HAVING count(*) > 1"
        case "enum":
            # NULL is outside the domain and never violates it. A member the
            # column cannot be compared with (INTEGER against 'a') equals nothing.
            column = columns[0]
            matches = " OR ".join(f"coalesce(try({column} = {value.sql()}), false)" for value in marker.values)
            query = f"SELECT {column} FROM {relation} WHERE {all_present} AND NOT ({matches})"
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
            assert_never(kind)
    return _with_context(select, query)


def _quoted(name: str) -> str:
    return exp.to_identifier(name, quoted=True).sql(dialect="duckdb")


def _join_violation(join: exp.Join, property: Property) -> str:
    """Counts of each input's rows against the join made LEFT (for the left
    input) or RIGHT (for the right input).

    Made LEFT, every left row contributes max(1, matches) rows, so the counts
    differ exactly when some left row matches more than once (it is repeated);
    made RIGHT, likewise for right rows. For exactly one match, the right
    input also carries a flag that is null exactly on the row a left row with
    no match is null-extended with.
    """
    select = join.parent
    assert isinstance(select, exp.Select), "an asserted join is one of a Select's joins"
    joins = select.args["joins"]
    position = next(index for index, candidate in enumerate(joins) if candidate is join)
    source = (select.args.get("from_") or select.args["from"]).this
    exactly_one = isinstance(property, ExactlyOneJoin)

    def from_joins(chosen: list[exp.Join]) -> str:
        query = exp.select("*").from_(source.copy())
        query.set("joins", chosen)
        return query.sql(dialect="duckdb")

    def made(side: str) -> exp.Join:
        outer = join.copy()
        outer.set("side", side)
        outer.set("kind", None)
        if not (outer.args.get("on") or outer.args.get("using") or outer.args.get("method")):
            outer.set("on", exp.true())
        return outer

    as_left, as_right = made("LEFT"), made("RIGHT")
    if exactly_one:
        # The flagged right input keeps its name, so the predicate still reads its columns.
        right = as_left.this
        flagged = exp.select("*", f"1 AS {_MATCHED}").from_(right.copy()).subquery(exp.to_identifier(right.alias_or_name))
        as_left.set("this", flagged)
    earlier = [previous.copy() for previous in joins[:position]]
    left = from_joins(earlier)
    joined = from_joins([*earlier, as_left])
    right_rows = exp.select("*").from_(join.this.copy()).sql(dialect="duckdb")
    joined_right = from_joins([*(previous.copy() for previous in joins[:position]), as_right])
    counts = (
        f"(SELECT count(*) FROM ({left})) AS left_rows, (SELECT count(*) FROM ({joined})) AS joined_rows, "
        f"(SELECT count(*) FROM ({right_rows})) AS right_rows, (SELECT count(*) FROM ({joined_right})) AS joined_right_rows"
    )
    left_repeated, right_repeated = "joined_rows > left_rows", "joined_right_rows > right_rows"
    match property:
        case ManyToOneJoin() | ExactlyOneJoin():
            violated = left_repeated
        case OneToManyJoin():
            violated = right_repeated
        case OneToOneJoin():
            violated = f"{left_repeated} OR {right_repeated}"
        case UniqueJoin():
            violated = f"{left_repeated} AND {right_repeated}"
        case _:
            raise TypeError(f"Not a join Property: {type(property).__name__}")
    if exactly_one:
        counts += f", (SELECT count(*) FROM ({joined}) WHERE {_MATCHED} IS NULL) AS unmatched_rows"
        violated += " OR unmatched_rows > 0"
    query = f"SELECT * FROM (SELECT {counts}) WHERE {violated}"
    # From the join, so the Select's own WITH is in scope: the queries above
    # are new Selects that do not carry it.
    return _with_context(join, query)


def _case_violation(case: exp.Case) -> str:
    """Non-null operand values that reach no WHEN arm, on every row the CASE
    is evaluated for.

    In a Select's WHERE that is every row of its FROM and joins; elsewhere in
    the Select, every such row passing WHERE; in a join's ON, every pairing of the rows joined so far
    with the joined source, matched or not. A CASE elsewhere is not judged.
    The CASE's own comparison decides a match, so `ELSE` marks a violation.
    """
    operand = case.this.sql(dialect="duckdb")
    arms = " ".join(f"WHEN {arm.this.sql(dialect='duckdb')} THEN FALSE" for arm in case.args["ifs"])
    unmatched = exp.condition(f"({operand}) IS NOT NULL AND CASE {operand} {arms} ELSE TRUE END")
    child, parent = case, case.parent
    while not isinstance(parent, exp.Select | exp.Join):
        assert parent is not None, "an asserted CASE is inside a Select"
        child, parent = parent, parent.parent
    select = parent if isinstance(parent, exp.Select) else parent.parent
    assert isinstance(select, exp.Select), "a join is one of a Select's joins"
    source = (select.args.get("from_") or select.args["from"]).this
    joins = select.args.get("joins") or []
    query = exp.select(operand).from_(source.copy())
    match parent:
        case exp.Join() if child is parent.args.get("on"):
            position = next(index for index, candidate in enumerate(joins) if candidate is parent)
            paired = exp.Join(this=parent.this.copy(), kind="CROSS")
            query.set("joins", [*(earlier.copy() for earlier in joins[:position]), paired])
            query = query.where(unmatched)
        case exp.Select() if child is parent.args.get("where"):
            # WHERE evaluates the CASE on rows it has yet to filter.
            query.set("joins", [join.copy() for join in joins])
            query = query.where(unmatched)
        case exp.Select() if child is not parent.args.get("having"):
            query.set("joins", [join.copy() for join in joins])
            where = select.args.get("where")
            query = query.where(where.this.copy() if where is not None else exp.true()).where(unmatched)
        case _:
            raise TypeError(f"an asserted CASE in {child.key} is not judged")
    # From the CASE, so the Select's own WITH is in scope.
    return _with_context(case, query.sql(dialect="duckdb"))


# The flag column an exactly-one check adds to the right input of its join.
_MATCHED = '"__sqlassert_matched"'


def _with_context(node: exp.Expression, query: str) -> str:
    """`query` prefixed with every CTE in scope at `node` from enclosing queries.

    A CTE whose own body holds `node` brings only the CTEs defined before it.
    """
    ctes: list[exp.Expression] = []
    recursive = False
    # The node's own CTEs too: a Foreign Key may reference one of them.
    own = node.args.get("with_") or node.args.get("with")
    if isinstance(own, exp.With):
        ctes = list(own.expressions)
        recursive = bool(own.args.get("recursive"))
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


class _Invalid(Exception):
    """An instance the program's constraints or declarations rule out."""


class _Database:
    """One in-memory DuckDB holding the program's DDL, refilled per example."""

    def __init__(self, parsed: ParsedProgram) -> None:
        self.connection = duckdb.connect(config={"threads": 1})
        self._create(parsed.create_statements)
        numbers, strings = _literals(parsed)
        self.tables = tuple(self._tables(numbers, strings))
        self.foreign_keys = tuple(self._foreign_key_checks(parsed.create_statements))
        for check in self.foreign_keys:
            self.connection.execute(check)

    def _foreign_key_checks(self, statements: tuple[exp.Expression, ...]) -> list[str]:
        """DDL Foreign Keys checked as coverage queries after the whole instance is loaded."""
        checks = []
        for statement in statements:
            for reference in statement.find_all(exp.Reference):
                source = statement.this.this if isinstance(statement.this, exp.Schema) else statement.this
                match reference.parent:
                    case exp.ForeignKey(expressions=columns):
                        names = tuple(column.name for column in columns)
                    case exp.ColumnConstraint(parent=exp.ColumnDef() as column):
                        names = (column.name,)
                    case _:
                        raise TypeError(f"Unhandled REFERENCES parent: {type(reference.parent).__name__}")
                target = reference.this
                if isinstance(target, exp.Schema):
                    referenced = tuple(column.name for column in target.expressions)
                    target = target.this
                else:
                    row = self.connection.execute(
                        "SELECT constraint_column_names FROM duckdb_constraints() "
                        "WHERE schema_name = ? AND table_name = ? AND constraint_type = 'PRIMARY KEY'",
                        [target.db or "main", target.name],
                    ).fetchone()
                    if row is None:
                        raise duckdb.CatalogException(f"REFERENCES {target.sql()} names no columns and its target has no primary key")
                    referenced = tuple(row[0])
                if len(names) != len(referenced):
                    raise duckdb.CatalogException("A Foreign Key must pair equally many referencing and referenced columns")
                marker = ColumnMarker("declared-foreign-key", names, Span(0, 0, 0, 0), target.sql(dialect="duckdb"), referenced)
                checks.append(_violation_query(exp.select("*").from_(source.copy()), marker, "coverage"))
        return checks

    def _create(self, statements: tuple[exp.Expression, ...]) -> None:
        """Run the DDL. A program may reference a relation declared later, which
        DuckDB rejects, so a failed statement is retried once the others ran."""
        pending = [sql for statement in statements if (sql := _runnable_create(statement))]
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

    def search(self, violated: Callable[[Instance], bool], max_examples: int) -> SearchResult:
        """Search valid loaded instances, distinguishing exhaustion from insufficient data."""
        strategy = st.fixed_dictionaries({
            table.name: st.lists(st.tuples(*(st.sampled_from(domain) for domain in table.domains)), max_size=MAX_ROWS)
            .map(lambda rows, table=table: _respecting_keys(rows, table.keys))
            for table in self.tables
        })
        attempted = valid = 0

        def checked(instance: Instance) -> bool:
            nonlocal attempted, valid
            attempted += 1
            try:
                self.load(instance)
                for check in self.foreign_keys:
                    if self.connection.execute(check).fetchall():
                        raise _Invalid
                result = violated(instance)
            except duckdb.Error as error:
                # Other DDL constraints or query casts may reject this data.
                raise _Invalid from error
            valid += 1
            return result

        def searched(instance: Instance) -> bool:
            try:
                return checked(instance)
            except _Invalid:
                assume(False)
                raise

        # Edge cases first, deterministically: a random search can miss the
        # one shape a counterexample needs (two rows sharing a value, say).
        for instance in _edge_instances(self.tables):
            try:
                if checked(instance):
                    return SearchResult("counterexample", instance, attempted, valid)
            except _Invalid:
                continue

        try:
            instance = find(strategy, searched, settings=settings(
                max_examples=max_examples, database=None, deadline=None, suppress_health_check=list(HealthCheck),
            ))
        except NoSuchExample:
            return SearchResult("no-counterexample", None, attempted, valid)
        except Unsatisfiable:
            return SearchResult("insufficient-data", None, attempted, valid)
        return SearchResult("counterexample", instance, attempted, valid)

    def violated(self, declarations: list[str], check: str) -> bool:
        for declaration in declarations:
            if self.connection.execute(declaration).fetchall():
                raise _Invalid
        return bool(self.connection.execute(check).fetchall())

    def load(self, instance: Instance) -> None:
        for table in self.tables:
            self.connection.execute(f"DELETE FROM {table.name}")
            rows = instance[table.name]
            if rows:
                placeholders = ", ".join("?" for _ in table.columns)
                self.connection.executemany(f"INSERT INTO {table.name} VALUES ({placeholders})", rows)


def _runnable_create(statement: exp.Expression) -> str:
    """The statement as DuckDB runs it. A table created from a query becomes a
    view, so its contents follow from generated data instead of being generated.
    DuckDB cannot ALTER TABLE ... ADD UNIQUE, so that becomes a unique index.
    Foreign Keys are removed here and checked after loading the complete instance."""
    statement = statement.transform(lambda node: None if (
        isinstance(node, exp.ForeignKey)
        or isinstance(node, exp.ColumnConstraint) and isinstance(node.args.get("kind"), exp.Reference)
    ) else node)
    for constraint in reversed(list(statement.find_all(exp.Constraint, exp.AddConstraint))):
        if not constraint.expressions:
            constraint.pop()
    if isinstance(statement, exp.Alter) and not statement.args.get("actions"):
        return ""
    if isinstance(statement, exp.Create) and (statement.kind or "").upper() == "TABLE" and statement.expression:
        statement = statement.copy()
        statement.set("kind", "VIEW")
    if isinstance(statement, exp.Alter):
        indexes = _unique_indexes(statement)
        if indexes is not None:
            return "; ".join(indexes)
    return statement.sql(dialect="duckdb")


def _unique_indexes(alter: exp.Alter) -> list[str] | None:
    """`ALTER TABLE t ADD UNIQUE (...)`, each constraint as `CREATE UNIQUE INDEX`,
    or None when the statement does anything else."""
    table = alter.this.sql(dialect="duckdb")
    indexes = []
    for action in alter.args.get("actions") or ():
        constraints = action.expressions if isinstance(action, exp.AddConstraint) else [action]
        for constraint in constraints:
            if isinstance(constraint, exp.Constraint) and len(constraint.expressions) == 1:
                constraint = constraint.expressions[0]
            if not (isinstance(constraint, exp.UniqueColumnConstraint) and isinstance(constraint.this, exp.Schema)):
                return None
            columns = ", ".join(column.sql(dialect="duckdb") for column in constraint.this.expressions)
            indexes.append(f"CREATE UNIQUE INDEX sqlassert_unique_{id(constraint)} ON {table} ({columns})")
    return indexes or None


# More combinations than this and the edge cases vary one table at a time.
EDGE_LIMIT = 400


def _edge_rows(table: _Table) -> list[list[tuple]]:
    """Row sets that tend to break a Property: no rows, one row, a duplicate,
    two rows that differ everywhere, two that differ in one column only, NULLs,
    and blank values (0, '', FALSE). Key-violating rows are dropped, as in the
    random search."""
    first = tuple(domain[0] for domain in table.domains)
    second = tuple(domain[1] if len(domain) > 1 else domain[0] for domain in table.domains)
    nulls = tuple(None if None in domain else domain[0] for domain in table.domains)
    blank = tuple(_blank(domain[0]) for domain in table.domains)
    one_column_differs = [
        [first, first[:index] + (second[index],) + first[index + 1:]] for index in range(len(first))
    ]
    shapes = [[], [first], [first, first], [first, second], *one_column_differs, [nulls], [first, nulls], [blank]]
    unique: list[list[tuple]] = []
    for rows in shapes:
        rows = _respecting_keys(rows, table.keys)
        if rows not in unique:
            unique.append(rows)
    return unique


def _blank(value: object) -> object:
    """The type's empty value: 0, '', or FALSE (a date column keeps its value)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0
    if isinstance(value, str) and not _is_date(value):
        return ""
    return value


def _is_date(value: str) -> bool:
    return len(value) == 10 and value[4] == "-" and value[7] == "-"


def _edge_instances(tables: Sequence[_Table]) -> list[Instance]:
    """Every combination of each table's edge row sets, or, when that is more
    than EDGE_LIMIT, each table's edge sets with every other table held at one
    of a few base sets (one row, a duplicate, two differing rows)."""
    per_table = [_edge_rows(table) for table in tables]
    total = 1
    for rows in per_table:
        total *= len(rows)
    if total <= EDGE_LIMIT:
        combinations = list(itertools.product(*per_table))
    else:
        combinations = []
        for index, own in enumerate(per_table):
            for base in (1, 2, 3):
                for rows in own:
                    combinations.append(tuple(
                        rows if other == index else others[min(base, len(others) - 1)]
                        for other, others in enumerate(per_table)
                    ))
    instances: list[Instance] = []
    for combination in combinations:
        instance = {table.name: list(rows) for table, rows in zip(tables, combination)}
        if instance not in instances:
            instances.append(instance)
    return instances


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
        marked = [value for select in statement.find_all(exp.Select) for marker in column_markers(select) for value in marker.values]
        written = [value for literal in statement.find_all(exp.Literal, exp.Neg) if (value := literal_value(literal)) is not None]
        for value in (*written, *marked):
            match value.type:
                case "string":
                    strings.append(value.text)
                case "numeric":
                    number = float(value.text)
                    numbers.append(int(number) if number.is_integer() else number)
                    # A text column compared with a number is cast to it.
                    strings.append(value.text)
                case _:
                    pass  # A boolean column already takes both values.
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
