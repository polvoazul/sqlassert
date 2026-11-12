"""Certify sqlassert's dialect facts against a real engine.

Every claim `sqlassert/dialects.py` makes about a dialect, and every Builtin
fact `ir/convert.dialect_builtins` derives from it, is a Fact here, with a
probe generated from it: SQL whose result confirms or refutes the claim on an
engine. `docs/dialect-provenance.json` records where each fact comes from:
`{"engine": [versions it passed on]}`, `{"undefined": [versions that cannot
express its construct]}` (both, when versions differ), or `{"doc": url,
"read": version}`. An undefined fact claims nothing on that version: if a
later one supports the construct, its probe runs there and decides.

The runner probes one dialect over a DB-API connection and prints PASS, FAIL,
VACUOUS (the engine cannot express the construct) or ERROR (the probe could
not run) per fact. Unless `--dry-run`, it records the engine version into the
provenance of each fact that passed (`engine`) or was vacuous (`undefined`),
and removes it from each that failed; it never adds or strengthens a fact. A FAIL names
the weaker setting to apply by hand in `dialects.py`. Facts are probed under
the engine's default settings only: sqlassert models each engine's latest
release under its defaults, strict modes on (ADR 0006). Weak settings claim
nothing; the runner reports, as STRONGER, one the engine would also satisfy,
to strengthen by hand when that engine is the latest release.

    python scripts/certify_dialects.py duckdb
    python scripts/certify_dialects.py sqlite
    python scripts/certify_dialects.py postgres --driver psycopg \\
        --connect 'host=127.0.0.1 port=55432 user=postgres password=probe'
    python scripts/certify_dialects.py mysql --driver pymysql \\
        --connect '{"host": "127.0.0.1", "port": 53306, "user": "root", "password": "probe", "database": "probe"}'
    python scripts/certify_dialects.py snowflake --list   # the probes, no connection

`docs/dialects.md` ("Certifying facts") has the docker setup.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import sqlglot
from sqlglot import Expr, exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlassert.dialects import MODELLED_DIALECTS, DialectSemantics, semantics  # noqa: E402
from sqlassert.ir import model as ir  # noqa: E402
from sqlassert.ir.convert import dialect_builtins  # noqa: E402
from sqlassert.sql_parse import sqlassert_dialect  # noqa: E402

PROVENANCE = ROOT / "docs" / "dialect-provenance.json"

VERSION_SQL: dict[str, str] = {
    "duckdb": "SELECT version()",
    "sqlite": "SELECT sqlite_version()",
    "postgres": "SHOW server_version",
    "mysql": "SELECT version()",
    "clickhouse": "SELECT version()",
    "snowflake": "SELECT CURRENT_VERSION()",
    "redshift": "SELECT version()",
    "databricks": "SELECT current_version().dbr_version",
    "spark": "SELECT version()",
    "hive": "SELECT version()",
    "oracle": "SELECT version_full FROM product_component_version",
    "trino": "SELECT version()",
}

# Appended to each probe table's CREATE: ClickHouse needs a table engine.
TABLE_SUFFIX: dict[str, str] = {"clickhouse": " ENGINE = Memory"}

type Expect = Literal["true", "error or true"]
type Outcome = Literal["PASS", "VACUOUS", "FAIL", "ERROR"]


@dataclass(frozen=True)
class Check:
    """`guard` runs the construct the check is about with ordinary values: an
    engine that rejects it cannot have a program that uses it, so the check is
    vacuous there (`^` in SQLite, an ENUM column in Postgres); so is one with a
    `problem` (no SQL the dialect parses to the construct). Then `setup` must
    succeed and `attempts` may fail (rejecting them is the point), and `query`
    returns one value, or raises, which `expect` judges. `tables` are dropped
    before and after."""

    query: str
    expect: Expect
    guard: tuple[str, ...] = ()
    setup: tuple[str, ...] = ()
    attempts: tuple[str, ...] = ()
    tables: tuple[str, ...] = ()
    problem: str = ""
    # An error whose message matches is a pass; any other error fails. A check
    # that declares none fails on every error.
    accepted_error: str = ""


@dataclass(frozen=True)
class Fact:
    """One claim about a dialect, its probe (every check must hold), and what
    to set when it fails."""

    name: str
    weaken: str
    checks: tuple[Check, ...]


# Facts --------------------------------------------------------------------


def facts(dialect: str, meaning: DialectSemantics | None = None) -> tuple[Fact, ...]:
    """Every claim `meaning` (the dialect's semantics by default) makes, each with its probe."""
    meaning = semantics(dialect) if meaning is None else meaning
    found = [*_builtin_facts(dialect, meaning), *_flag_facts(dialect, meaning)]
    return tuple(sorted(found, key=lambda fact: fact.name))


def candidate_facts(dialect: str) -> tuple[Fact, ...]:
    """The facts the strong reading of each weak flag would add: probed only to report."""
    meaning = semantics(dialect)
    strong = replace(
        meaning, checked_arithmetic=True, checked_division=True, casts_raise=True, text_is_binary=True, unscaled_decimal=False,
        zoned_timestamp_is_instant=True,
    )
    claimed = {fact.name for fact in facts(dialect, meaning)}
    return tuple(fact for fact in facts(dialect, strong) if fact.name not in claimed)


def _cast(value: Expr | str, to: str) -> Expr:
    this = exp.Literal.string(value) if isinstance(value, str) else value
    return exp.Cast(this=this, to=exp.DataType.build(to))


def _n(value: int) -> Expr:
    return exp.Literal.number(value)


_BIGINT_MAX = _cast("9223372036854775807", "BIGINT")
_BIGINT_MIN = _cast("-9223372036854775808", "BIGINT")
_HUGE_DOUBLE = exp.Mul(this=_cast("1e308", "DOUBLE"), expression=_cast("10", "DOUBLE"))
_X = exp.Literal.string("x")


@dataclass(frozen=True)
class _Shape:
    """How to call a Builtin: its parameters' type (for a typed NULL), ordinary
    arguments, arguments that overflow (an unchecked engine wraps, saturates
    or returns NULL) and arguments a failed implicit cast or a bad value makes
    NULL in an engine that does not raise. A variadic Builtin's `ordinary`
    holds its fixed parameters and one repetition."""

    type: str
    ordinary: tuple[Expr, ...]
    overflow: tuple[tuple[Expr, ...], ...] = ()
    bad: tuple[tuple[Expr, ...], ...] = ()
    extra: tuple[tuple[str, Expr], ...] = ()


_IMPLICIT_CAST = _Shape("INT", (_n(1), _n(2)), bad=((_n(1), _X),))
_SHAPES: dict[str, _Shape] = {
    **{name: _IMPLICIT_CAST for name in ("<>", ">", ">=", "<", "<=", "&", "|", "^", "<<", ">>")},
    "LIKE": _Shape("VARCHAR", (exp.Literal.string("a"), exp.Literal.string("a")), bad=((_n(1), _X),)),
    "ILIKE": _Shape("VARCHAR", (exp.Literal.string("a"), exp.Literal.string("a")), bad=((_n(1), _X),)),
    "+": _Shape("BIGINT", (_n(1), _n(2)), overflow=((_BIGINT_MAX, _n(1)),), bad=((_HUGE_DOUBLE, exp.Neg(this=_HUGE_DOUBLE)),)),
    "-": _Shape("BIGINT", (_n(1), _n(2)), overflow=((_BIGINT_MIN, _n(1)),), bad=((_HUGE_DOUBLE, _HUGE_DOUBLE),)),
    "*": _Shape("BIGINT", (_n(1), _n(2)), overflow=((_BIGINT_MAX, _n(2)),), bad=((_HUGE_DOUBLE, _cast("0", "DOUBLE")),)),
    "unary -": _Shape("BIGINT", (_n(1),), overflow=((_BIGINT_MIN,),)),
    "/": _Shape("INT", (_n(4), _n(2)), bad=((_n(1), _n(0)),)),
    "//": _Shape("INT", (_n(4), _n(2)), bad=((_n(1), _n(0)),)),
    "%": _Shape("INT", (_n(4), _n(2)), bad=((_n(1), _n(0)),)),
    "NOT": _Shape("BOOLEAN", (exp.true(),), bad=((_X,),)),
    "~": _Shape("INT", (_n(1),), bad=((_X,),)),
    "CAST": _Shape(
        "VARCHAR", (exp.Literal.string("1"),), bad=((_X,), (exp.Literal.string("99999999999999999999"),)),
        extra=(("to", exp.DataType.build("BIGINT")),),
    ),
    "BETWEEN": _Shape("INT", (_n(1), _n(0), _n(2)), bad=((_n(1), _X, _n(2)),)),
    "||": _Shape("VARCHAR", (exp.Literal.string("a"), exp.Literal.string("b"))),
    "concat": _Shape("VARCHAR", (exp.Literal.string("a"),)),
    "concat_ws": _Shape("VARCHAR", (exp.Literal.string(","), exp.Literal.string("a"))),
    "lower": _Shape("VARCHAR", (exp.Literal.string("A"),)),
    "upper": _Shape("VARCHAR", (exp.Literal.string("a"),)),
    "length": _Shape("VARCHAR", (exp.Literal.string("abc"),)),
    "trim": _Shape("VARCHAR", (exp.Literal.string(" a "),), bad=((exp.Literal.string(" "),),)),
    "abs": _Shape("BIGINT", (_n(-1),), overflow=((_BIGINT_MIN,),), bad=((_X,),)),
    "floor": _Shape("DOUBLE", (exp.Literal.number("1.5"),), bad=((_X,),)),
    "ceil": _Shape("DOUBLE", (exp.Literal.number("1.5"),), bad=((_X,),)),
    "nullif": _Shape("INT", (_n(1), _n(2))),
    "greatest": _Shape("INT", (_n(1), _n(2))),
    "least": _Shape("INT", (_n(1), _n(2))),
    "[]": _Shape("INT[]", (exp.Array(expressions=[_n(1), _n(2)]), _n(1))),
}

# The flag each Builtin's totality and injectivity rest on (`dialect_builtins`).
_ARITHMETIC = {"+", "-", "*", "unary -", "CAST", "abs", "floor", "ceil"}
_DIVISION = {"/", "//", "%"}
_TEXT = {
    "||": "pipes=None", "concat": "concat=None", "concat_ws": "concat_ws=None",
    "trim": "empty_string_is_null=True",
    **{name: f"make {name} non-total in ir/convert.dialect_builtins" for name in ("lower", "upper", "length")},
}


def _total_flag(name: str) -> str:
    if name in _ARITHMETIC:
        return "checked_arithmetic=False"
    if name in _DIVISION:
        return "checked_division=False"
    return _TEXT.get(name, "casts_raise=False")


def _builtin_facts(dialect: str, meaning: DialectSemantics) -> Iterable[Fact]:
    for node, (builtin, keys) in dialect_builtins(meaning).items():
        shape = _SHAPES[builtin.name]
        variadic = isinstance(builtin, ir.BuiltinVariadic)
        count = len(shape.ordinary) + (1 if variadic else 0)  # two repetitions
        ordinary = shape.ordinary + (shape.ordinary[-1:] if variadic else ())
        strict = builtin.strict_flags(count)
        total = builtin.total_flags(count)

        def call(arguments: Sequence[Expr]) -> Expr:
            args: dict[str, Any] = dict(shape.extra)
            if node is exp.ConcatWs:
                # SQLGlot's node is NULL-strict and writes a dialect whose concat_ws skips
                # NULL as a CASE around it; `coalesce` says the node is that dialect's own,
                # so the probe runs the engine's concat_ws, not SQLGlot's emulation.
                args["coalesce"] = sqlglot.Dialect.get_or_raise(sqlassert_dialect(dialect)).CONCAT_WS_COALESCE
            if node in (exp.Greatest, exp.Least):
                # Likewise: SQLGlot writes a NULL-strict CASE unless the node skips NULLs as the dialect's own does.
                args["ignore_nulls"] = sqlglot.parse_one("greatest(a, b)", read=sqlassert_dialect(dialect)).args.get("ignore_nulls")
            if variadic:
                # The fixed parameters by their own keys (none when one list holds all), then the list.
                fixed = len(keys) - 1
                args.update(zip(keys[:fixed], arguments[:fixed], strict=True))
                args[keys[-1]] = list(arguments[fixed:])
            else:
                # SQLGlot keeps `expressions` as a list (a subscript's index).
                args.update((key, [argument] if key == "expressions" else argument) for key, argument in zip(keys, arguments, strict=True))
            if node is exp.Bracket:
                # PostgreSQL subscripts a CAST only in parentheses.
                args["this"] = exp.Paren(this=args["this"])
            return node(**args)

        def null_at(arguments: Sequence[Expr], positions: Iterable[int]) -> tuple[Expr, ...]:
            nulls = set(positions)
            return tuple(_cast(exp.null(), shape.type) if i in nulls else argument for i, argument in enumerate(arguments))

        label = builtin.name
        guards = _spell(call(ordinary), label, dialect)

        def probe(arguments: Sequence[Expr], expect: Expect, exact: int | None = None, accepted_error: str = "") -> Iterable[Check]:
            """One check per way to write the call, guarded by the same way with ordinary arguments.
            With `exact`, the check passes when the call raises or returns exactly that
            integer, as text (a float is not exact): an engine may widen the type (DuckDB 2.0 returns `-MIN` as a HUGEINT)."""
            spellings = _spell(call(arguments), label, dialect)
            if not spellings:
                yield Check("", expect, problem=f"no {dialect} SQL parses to {node.__name__}")
            for key, text in spellings.items():
                query = (
                    f"SELECT CASE WHEN CAST(({text}) AS VARCHAR(40)) = '{exact}' THEN 1 ELSE 0 END" if exact is not None
                    else _is_null(text) if expect == "true"
                    else _is_not_null(text)
                )
                yield Check(query, expect, guard=(f"SELECT {guards[key]}",) if key in guards else (), accepted_error=accepted_error)

        strict_positions = [i for i, flag in enumerate(strict) if flag]
        if strict_positions:
            checks = tuple(check for i in strict_positions for check in probe(null_at(ordinary, [i]), "true"))
            yield Fact(f"strict({label})", f"make {label} non-strict in ir/convert.dialect_builtins", checks)
        if total is not None:
            free = [i for i, flag in enumerate(total) if not flag]
            cases = [ordinary, *(case + ordinary[len(case):] for case in (*shape.overflow, *shape.bad))]
            checks = tuple(check for case in cases for check in probe(null_at(case, free), "error or true", accepted_error=_DATA_ERROR))
            yield Fact(f"total({label})", _total_flag(label), checks)
        if builtin.skips_null:
            # Non-null whenever any argument is: every other one NULL.
            checks = tuple(
                check for i in range(count)
                for check in probe(null_at(ordinary, [j for j in range(count) if j != i]), "error or true", accepted_error=_DATA_ERROR)
            )
            yield Fact(f"skips_null({label})", "greatest=None", checks)
        if builtin.injective:
            checks = tuple(check for case in shape.overflow for check in probe(case, "error or true", exact=_exact(label, case), accepted_error=_OVERFLOW_ERROR))
            yield Fact(f"injective({label})", "checked_arithmetic=False", checks)


# The messages of an error that is the engine checking a value: integer overflow, a
# value out of range, a division by zero, a failed cast, and (for `total`: a failed
# implicit cast is the engine refusing the program) a type error at bind time. Any
# other error (syntax, an unknown function) fails the probe.
_OVERFLOW_ERROR = r"(?i)overflow|out of range|out-of-range"
_DATA_ERROR = (
    _OVERFLOW_ERROR + r"|division by (zero|0)|divide by zero|divided by zero|cannot be cast|cannot cast|could not convert"
    r"|conversion error|invalid input syntax|invalid (numeric|value|number|literal)|cannot parse|cannot convert|incorrect .* value|truncated"
    r"|explicit type casts|no function matches|best candidate|operator does not exist|cannot be applied|no matching signature|illegal_type_of_argument"
    # Trino's type errors: a known operator or function given an argument of the wrong type.
    r"|cannot apply operator|unexpected parameters \(.*\) for function|must evaluate to a|cannot check if"
)
_LEAST, _GREATEST = -(2**63), 2**63 - 1
_EXACT = {"+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b, "unary -": lambda a: -a}


def _exact(name: str, case: Sequence[Expr]) -> int:
    """The mathematically exact result of an overflow case."""
    operands = [_LEAST if argument is _BIGINT_MIN else _GREATEST if argument is _BIGINT_MAX else int(argument.name) for argument in case]
    return _EXACT[name](*operands)


# Spellings SQLGlot does not write but parses to the Builtin's node.
_SPELLINGS: dict[str, tuple[str, ...]] = {"//": ("{0} DIV {1}",)}


def _spell(node: Expr, name: str, dialect: str) -> dict[str, str]:
    """`node` as the dialect's users may write it, by way of writing: each SQL
    that sqlassert parses back to the same node type (SQLGlot writes DuckDB's
    `concat()` as `||`, which treats NULL differently; MySQL writes integer
    division `a DIV b`)."""
    arguments = [value for value in node.args.values() if isinstance(value, Expr)]
    listed = node.args.get("expressions") or []
    written = [argument.sql(dialect=dialect) for argument in [*arguments, *listed]]
    spellings = {"sqlglot": node.sql(dialect=dialect)}
    spellings.update((f"form {i}", spelling.format(*written)) for i, spelling in enumerate(_SPELLINGS.get(name, ())))
    if name.isidentifier():  # A function: also its own name with the arguments as written.
        spellings["function"] = f"{name}({', '.join(written)})"
    found: dict[str, str] = {}
    for key, text in spellings.items():
        try:
            parsed = sqlglot.parse_one(f"SELECT {text}", read=sqlassert_dialect(dialect))
        except sqlglot.errors.SqlglotError:
            continue
        # Postgres `DIV(a, b)` is a CAST of the IntDiv node.
        if isinstance(parsed, exp.Select) and any(type(each) is type(node) for each in parsed.expressions[0].walk()):
            if text not in found.values():
                found[key] = text
    return found


def _is_null(text: str) -> str:
    return f"SELECT CASE WHEN ({text}) IS NULL THEN 1 ELSE 0 END"


def _is_not_null(text: str) -> str:
    return f"SELECT CASE WHEN ({text}) IS NULL THEN 0 ELSE 1 END"


def _query(sql: str, dialect: str) -> str:
    """A query written in SQLGlot's own dialect, in `dialect`'s."""
    return sqlglot.transpile(sql, write=dialect)[0]


def _table(dialect: str, name: str, columns: str, suffix: str = "") -> str:
    """A probe table's CREATE, written as the dialect's users write it (not transpiled)."""
    return f"CREATE TABLE {name} ({columns}){suffix or TABLE_SUFFIX.get(dialect, '')}"


def _identity_check(dialect: str, column: str) -> Check:
    """Equal text is identical: of 'a', 'A', 'a ' and 'á', only 'a' equals 'a'."""
    table = "sqlassert_probe_text"
    rows = ", ".join(f"('{text}')" for text in ("a", "A", "a ", "á"))
    return Check(
        _query(f"SELECT CASE WHEN count(*) = 1 THEN 1 ELSE 0 END FROM {table} WHERE a = 'a'", dialect), "true",
        setup=(_table(dialect, table, f"a {column}"), f"INSERT INTO {table} (a) VALUES {rows}"), tables=(table,),
    )


def _foreign_key_check(dialect: str, reference: str, suffix: str = "") -> Check:
    """No child row survives without its parent: an orphan insert and a parent delete are rejected."""
    parent, child = "sqlassert_probe_parent", "sqlassert_probe_child"
    return Check(
        _query(f"SELECT CASE WHEN count(*) = 0 THEN 1 ELSE 0 END FROM {child} WHERE pid IS NOT NULL AND pid NOT IN (SELECT id FROM {parent})", dialect),
        "true",
        setup=(
            _table(dialect, parent, "id INT PRIMARY KEY", suffix),
            _table(dialect, child, f"id INT, pid INT{reference}", suffix),
            f"INSERT INTO {parent} (id) VALUES (1)",
            f"INSERT INTO {child} (id, pid) VALUES (1, 1)",
        ),
        attempts=(f"INSERT INTO {child} (id, pid) VALUES (2, 2)", f"DELETE FROM {parent} WHERE id = 1"),
        tables=(child, parent),
    )


def _not_null_key_check(dialect: str, columns: str, attempts: tuple[str, ...]) -> Check:
    """No primary key column holds NULL, whatever an INSERT of one does."""
    table = "sqlassert_probe_pk"
    return Check(
        _query(f"SELECT CASE WHEN count(*) = 0 THEN 1 ELSE 0 END FROM {table} WHERE a IS NULL OR b IS NULL", dialect), "true",
        setup=(_table(dialect, table, columns),),
        attempts=tuple(f"INSERT INTO {table} (a, b) VALUES {rows}" for rows in attempts),
        tables=(table,),
    )


def _flag_facts(dialect: str, meaning: DialectSemantics) -> Iterable[Fact]:
    if meaning.text_is_binary:
        yield Fact("text_is_binary", "text_is_binary=False", (_identity_check(dialect, "VARCHAR(10)"),))
    if not meaning.unscaled_decimal:
        table = "sqlassert_probe_decimal"
        check = Check(
            _query(f"SELECT CASE WHEN count(DISTINCT CAST(d AS VARCHAR(40))) = 1 THEN 1 ELSE 0 END FROM {table}", dialect), "true",
            setup=(_table(dialect, table, "d DECIMAL"), f"INSERT INTO {table} (d) VALUES (1.0), (1.00)"), tables=(table,),
        )
        yield Fact("unscaled_decimal=False", "unscaled_decimal=True", (check,))
    if meaning.zoned_timestamp_is_instant:
        table = "sqlassert_probe_zoned"
        zoned = exp.DataType.build("TIMESTAMPTZ").sql(dialect=dialect)
        rows = "('2024-01-01 00:00:00+00:00'), ('2024-01-01 01:00:00+01:00')"
        check = Check(
            _query(f"SELECT CASE WHEN count(DISTINCT CAST(t AS VARCHAR(60))) = 1 AND count(DISTINCT t) = 1 THEN 1 ELSE 0 END FROM {table}", dialect),
            "true", setup=(_table(dialect, table, f"t {zoned}"), f"INSERT INTO {table} (t) VALUES {rows}"), tables=(table,),
        )
        yield Fact("zoned_timestamp_is_instant", "zoned_timestamp_is_instant=False", (check,))
    if meaning.unique_keys == "enforced":
        table = "sqlassert_probe_key"
        check = Check(
            _query(f"SELECT CASE WHEN count(*) = count(DISTINCT id) AND count(u) = count(DISTINCT u) THEN 1 ELSE 0 END FROM {table}", dialect),
            "true",
            setup=(_table(dialect, table, "id INT PRIMARY KEY, u INT UNIQUE"), f"INSERT INTO {table} (id, u) VALUES (1, 1)"),
            attempts=tuple(f"INSERT INTO {table} (id, u) VALUES {rows}" for rows in ("(1, 2)", "(2, 1)", "(3, 3), (3, 3)", "(4, 4), (5, 4)")),
            tables=(table,),
        )
        yield Fact("unique_keys=enforced", "unique_keys='unenforced'", (check,))
        if meaning.primary_key_not_null:
            yield Fact("primary_key_not_null", "primary_key_not_null=False", (
                _not_null_key_check(dialect, "a INT PRIMARY KEY, b INT", ("(NULL, 1)", "(1, 1), (NULL, 2)")),
                _not_null_key_check(dialect, "a INT, b INT, PRIMARY KEY (a, b)", ("(NULL, 1)", "(1, NULL)", "(1, 1), (2, NULL)")),
            ))
    if meaning.foreign_keys == "enforced":
        yield Fact("foreign_keys=enforced", "foreign_keys='unenforced'", (_foreign_key_check(dialect, ", FOREIGN KEY (pid) REFERENCES sqlassert_probe_parent (id)"),))
        if meaning.inline_references:
            yield Fact("inline_references", "inline_references=False", (_foreign_key_check(dialect, " REFERENCES sqlassert_probe_parent (id)"),))
        for engine in sorted(meaning.foreign_key_engines or ()):
            check = _foreign_key_check(dialect, ", FOREIGN KEY (pid) REFERENCES sqlassert_probe_parent (id)", f" ENGINE = {engine}")
            yield Fact(f"foreign_key_engines:{engine}", f"drop {engine!r} from foreign_key_engines", (check,))
    # An ENUM holds only its members (as the dialect stores them), whatever is inserted.
    table = "sqlassert_probe_enum"
    stored = "'x', 'y'" if meaning.enum_strips_trailing_spaces else "'x', 'y '"
    check = Check(
        _query(f"SELECT CASE WHEN count(*) = 0 THEN 1 ELSE 0 END FROM {table} WHERE a NOT IN ({stored})", dialect), "true",
        guard=(_table(dialect, table, "a ENUM('x', 'y ')"),),
        attempts=tuple(f"INSERT INTO {table} (a) VALUES ('{value}')" for value in ("z", "", "y ", "y")),
        tables=(table,),
    )
    yield Fact("enum_members_only", "an ENUM holds a value outside its members (or keeps trailing spaces): model it", (check,))
    empty = meaning.empty_string_is_null
    check = Check(_is_null("''") if empty else _is_not_null("''"), "true")
    yield Fact(f"empty_string_is_null={empty}", f"empty_string_is_null={not empty}", (check,))


# Running ------------------------------------------------------------------


@dataclass(frozen=True)
class Result:
    fact: Fact
    outcome: Outcome
    detail: str = ""


class _ProbeError(Exception):
    pass


def _execute(cursor: Any, sql: str) -> list[tuple[Any, ...]]:
    cursor.execute(sql)
    return list(cursor.fetchall()) if cursor.description else []


def _drop(cursor: Any, tables: Iterable[str]) -> None:
    for table in tables:
        try:
            _execute(cursor, f"DROP TABLE IF EXISTS {table}")
        except Exception:  # noqa: BLE001 - best effort: the table may not exist
            pass


def _check(cursor: Any, check: Check) -> tuple[Outcome, str]:
    """Whether `check` holds (PASS or FAIL) or is VACUOUS; raises
    `_ProbeError` when its setup fails."""
    if check.problem:
        return "VACUOUS", check.problem
    _drop(cursor, check.tables)
    try:
        for statement in check.guard:
            try:
                _execute(cursor, statement)
            except Exception as error:  # noqa: BLE001 - any driver's error
                return "VACUOUS", f"the engine rejects {statement}: {_first_line(error)}"
        for statement in check.setup:
            try:
                _execute(cursor, statement)
            except Exception as error:  # noqa: BLE001 - any driver's error
                raise _ProbeError(f"{statement}: {_first_line(error)}") from error
        for statement in check.attempts:
            try:
                _execute(cursor, statement)
            except Exception:  # noqa: BLE001, S110 - rejecting it is the point
                pass
        try:
            rows = _execute(cursor, check.query)
        except Exception as error:  # noqa: BLE001 - any driver's error
            message = _first_line(error)
            accepted = bool(check.accepted_error) and re.search(check.accepted_error, str(error)) is not None
            return ("PASS" if accepted else "FAIL"), f"{check.query} raised {message}" + ("" if accepted else f" (accepted: {check.accepted_error!r})")
        value = rows[0][0] if rows and rows[0] else None
        truthy = value in (1, True) or value == "1"
        return ("PASS" if truthy else "FAIL"), f"{check.query} returned {value!r}"
    finally:
        _drop(cursor, check.tables)


def _first_line(error: BaseException) -> str:
    return (str(error).strip().splitlines() or [type(error).__name__])[0][:160]


def run(cursor: Any, dialect: str, probed: Sequence[Fact]) -> list[Result]:
    """Each fact's outcome under the engine's default settings."""
    return [_run_fact(cursor, fact) for fact in probed]


def _run_fact(cursor: Any, fact: Fact) -> Result:
    """FAIL if a check fails, ERROR if one cannot run, PASS if one ran and
    every one held, VACUOUS if none applied."""
    vacuous: list[str] = []
    for check in fact.checks:
        try:
            outcome, detail = _check(cursor, check)
        except _ProbeError as error:
            return Result(fact, "ERROR", str(error))
        if outcome == "FAIL":
            return Result(fact, "FAIL", detail)
        if outcome == "VACUOUS":
            vacuous.append(detail)
    if len(vacuous) == len(fact.checks):
        return Result(fact, "VACUOUS", vacuous[0])
    return Result(fact, "PASS")


def engine_version(cursor: Any, dialect: str) -> str:
    """`<engine> <version>`, e.g. `postgres 17.6`, `mariadb 11.4.3`."""
    value = str(_execute(cursor, VERSION_SQL[dialect])[0][0])
    engine = "mariadb" if "mariadb" in value.lower() else dialect
    match = re.search(r"\d+(\.\d+)*", value)
    return f"{engine} {match.group(0) if match else value}"


# Provenance ---------------------------------------------------------------


def load_provenance(path: Path = PROVENANCE) -> dict[str, dict[str, dict[str, Any]]]:
    return json.loads(path.read_text()) if path.exists() else {}


def write_provenance(provenance: dict[str, dict[str, dict[str, Any]]], path: Path = PROVENANCE) -> None:
    """One line per fact, dialects and facts sorted, so a change is a readable diff."""
    lines = ["{"]
    for i, dialect in enumerate(sorted(provenance)):
        lines.append(f"  {json.dumps(dialect)}: {{")
        entries = sorted(provenance[dialect].items())
        lines.extend(
            f"    {json.dumps(name)}: {json.dumps(entry, ensure_ascii=False)}{',' if j < len(entries) - 1 else ''}"
            for j, (name, entry) in enumerate(entries)
        )
        lines.append("  }" + ("," if i < len(provenance) - 1 else ""))
    lines.append("}")
    path.write_text("\n".join(lines) + "\n")


def record(provenance: dict[str, dict[str, dict[str, Any]]], dialect: str, version: str, results: Sequence[Result]) -> list[str]:
    """File `version` under each passing fact's `engine` versions and each
    vacuous fact's `undefined` ones (replacing a doc provenance), and remove it
    from each failing fact's; drop facts the dialect no longer has. An ERROR
    changes nothing. Returns what changed."""
    entries = provenance.setdefault(dialect, {})
    changes: list[str] = []
    current = {result.fact.name for result in results}
    for name in sorted(set(entries) - current):
        del entries[name]
        changes.append(f"dropped {name}: no longer a fact")
    for result in results:
        name, entry = result.fact.name, entries.get(result.fact.name) or {}
        lists = {key: [each for each in entry.get(key, ()) if each != version] for key in ("engine", "undefined")}
        match result.outcome:
            case "PASS":
                lists["engine"].append(version)
                change = f"{name}: certified on {version}"
            case "VACUOUS":
                lists["undefined"].append(version)
                change = f"{name}: undefined on {version}"
            case "FAIL":
                change = f"{name}: no longer certified on {version}"
            case "ERROR":
                continue
        updated = {key: sorted(versions) for key, versions in lists.items() if versions}
        if updated == entry:
            continue
        if updated:
            entries[name] = updated
        elif name in entries:
            del entries[name]
        changes.append(change)
    return changes


# Command line -------------------------------------------------------------


def _connect(dialect: str, driver: str | None, connect: str | None) -> Any:
    driver = driver or {"duckdb": "duckdb", "sqlite": "sqlite3"}.get(dialect)
    if driver is None:
        raise SystemExit(f"{dialect}: pass --driver (a DB-API module) and --connect")
    module = importlib.import_module(driver)
    if connect is None:
        connect = ":memory:" if driver in ("duckdb", "sqlite3") else ""
    if connect.lstrip().startswith("{"):
        connection = module.connect(**json.loads(connect))
    else:
        connection = module.connect(connect)
    # Each statement in its own transaction, so a rejected one rolls back alone.
    if driver in ("psycopg", "sqlite3"):
        connection.autocommit = True
    elif driver == "pymysql":
        connection.autocommit(True)
    return connection


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dialect", choices=sorted(MODELLED_DIALECTS))
    parser.add_argument("--driver", help="DB-API module (default: duckdb or sqlite3 for those dialects)")
    parser.add_argument("--connect", help="its connect() argument, or a JSON object of keyword arguments")
    parser.add_argument("--dry-run", action="store_true", help="probe, but record nothing")
    parser.add_argument("--list", action="store_true", help="print each fact and its probe; connect to nothing")
    options = parser.parse_args(argv)
    dialect = options.dialect
    probed = facts(dialect)
    if options.list:
        for fact in probed:
            print(f"{fact.name}  (fails: {fact.weaken})")
            for check in fact.checks:
                for statement in (*check.setup, *check.attempts):
                    print(f"    {statement};")
                print(f"    {check.query};  -- expect {check.expect}")
        return 0
    connection = _connect(dialect, options.driver, options.connect)
    cursor = connection.cursor()
    version = engine_version(cursor, dialect)
    print(f"{dialect} on {version}")
    results = run(cursor, dialect, probed)
    for result in results:
        hint = f"  -> set {result.fact.weaken}" if result.outcome == "FAIL" else ""
        print(f"{result.outcome:7} {result.fact.name}{hint}{'  ' + result.detail if result.detail else ''}")
    for result in run(cursor, dialect, candidate_facts(dialect)):
        if result.outcome == "PASS":
            print(f"STRONGER {result.fact.name}: holds on {version} too (not claimed: strengthen it by hand if {version} is the latest release)")
    failed = [result for result in results if result.outcome in ("FAIL", "ERROR")]
    if not options.dry_run:
        provenance = load_provenance()
        changes = record(provenance, dialect, version, results)
        write_provenance(provenance)
        print(*(f"recorded: {change}" for change in changes), sep="\n")
    print(f"{len(results) - len(failed)} of {len(results)} facts hold on {version}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
