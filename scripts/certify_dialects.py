"""Certify sqlassert's dialect facts against a real engine.

Every claim `sqlassert/dialects.py` makes about a dialect, and every Builtin
fact `ir/convert.dialect_builtins` derives from it, is a Fact here, with a
probe generated from it: SQL whose result confirms or refutes the claim on an
engine. `docs/dialect-provenance.json` records where each fact comes from:
`{"engine": [versions it passed on]}` or `{"doc": url, "read": version}`.

The runner probes one dialect over a DB-API connection and prints PASS, FAIL
or ERROR (the probe could not run) per fact. Unless `--dry-run`, it records
the engine version into the provenance of each fact that passed, and removes
it from each that failed; it never adds or strengthens a fact. A FAIL names
the weaker setting to apply by hand in `dialects.py`. A dialect's flags hold
under all its settings, so a fact must pass under the engine's defaults and
again after `PERMISSIVE` statements (MySQL `sql_mode = ''`, Spark ANSI
off). A setting that changes the default collation is out of scope
(docs/dialects.md). Weak settings claim nothing;
the runner only reports, as STRONGER, an engine that would also satisfy the
strong one (never acted on).

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
from sqlglot import exp

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlassert.dialects import MODELLED_DIALECTS, DialectSemantics, semantics  # noqa: E402
from sqlassert.ir import model as ir  # noqa: E402
from sqlassert.ir.convert import dialect_builtins  # noqa: E402
from sqlassert.sql_parse import sqlassert_dialect  # noqa: E402

PROVENANCE = ROOT / "docs" / "dialect-provenance.json"

# Statements after which every fact must still hold: the dialect's settings
# that weaken what its SQL means.
PERMISSIVE: dict[str, tuple[str, ...]] = {
    "mysql": ("SET SESSION sql_mode = ''",),
    "spark": ("SET spark.sql.ansi.enabled = false",),
    "spark2": ("SET spark.sql.ansi.enabled = false",),
    "databricks": ("SET ANSI_MODE = false",),
}

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
    "spark2": "SELECT version()",
    "hive": "SELECT version()",
    "oracle": "SELECT version_full FROM product_component_version",
}

# Appended to each probe table's CREATE: ClickHouse needs a table engine.
TABLE_SUFFIX: dict[str, str] = {"clickhouse": " ENGINE = Memory"}

type Expect = Literal["true", "error", "error or true"]
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
    )
    claimed = {fact.name for fact in facts(dialect, meaning)}
    return tuple(fact for fact in facts(dialect, strong) if fact.name not in claimed)


def _cast(value: exp.Expression | str, to: str) -> exp.Expression:
    this = exp.Literal.string(value) if isinstance(value, str) else value
    return exp.Cast(this=this, to=exp.DataType.build(to))


def _n(value: int) -> exp.Expression:
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
    ordinary: tuple[exp.Expression, ...]
    overflow: tuple[tuple[exp.Expression, ...], ...] = ()
    bad: tuple[tuple[exp.Expression, ...], ...] = ()
    extra: tuple[tuple[str, exp.Expression], ...] = ()


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
}

# The flag each Builtin's totality and injectivity rest on (`dialect_builtins`).
_ARITHMETIC = {"+", "-", "*", "unary -", "CAST"}
_DIVISION = {"/", "//", "%"}
_TEXT = {"||": "pipes=None", "concat": "concat=None", "concat_ws": "concat_ws=None"}


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

        def call(arguments: Sequence[exp.Expression]) -> exp.Expression:
            args: dict[str, Any] = dict(shape.extra)
            if variadic:
                args[keys[0]] = list(arguments)
            else:
                args.update(zip(keys, arguments, strict=True))
            return node(**args)

        def null_at(arguments: Sequence[exp.Expression], positions: Iterable[int]) -> tuple[exp.Expression, ...]:
            nulls = set(positions)
            return tuple(_cast(exp.null(), shape.type) if i in nulls else argument for i, argument in enumerate(arguments))

        label = builtin.name
        guards = _spell(call(ordinary), label, dialect)

        def probe(arguments: Sequence[exp.Expression], expect: Expect) -> Iterable[Check]:
            """One check per way to write the call, guarded by the same way with ordinary arguments."""
            spellings = _spell(call(arguments), label, dialect)
            if not spellings:
                yield Check("", expect, problem=f"no {dialect} SQL parses to {node.__name__}")
            for key, text in spellings.items():
                query = _is_null(text) if expect == "true" else _is_not_null(text) if expect == "error or true" else f"SELECT {text}"
                yield Check(query, expect, guard=(f"SELECT {guards[key]}",) if key in guards else ())

        strict_positions = [i for i, flag in enumerate(strict) if flag]
        if strict_positions:
            checks = tuple(check for i in strict_positions for check in probe(null_at(ordinary, [i]), "true"))
            yield Fact(f"strict({label})", f"make {label} non-strict in ir/convert.dialect_builtins", checks)
        if total is not None:
            free = [i for i, flag in enumerate(total) if not flag]
            cases = [ordinary, *(case + ordinary[len(case):] for case in (*shape.overflow, *shape.bad))]
            checks = tuple(check for case in cases for check in probe(null_at(case, free), "error or true"))
            yield Fact(f"total({label})", _total_flag(label), checks)
        if builtin.injective:
            checks = tuple(check for case in shape.overflow for check in probe(case, "error"))
            yield Fact(f"injective({label})", "checked_arithmetic=False", checks)


# Spellings SQLGlot does not write but parses to the Builtin's node.
_SPELLINGS: dict[str, tuple[str, ...]] = {"//": ("{0} DIV {1}",)}


def _spell(node: exp.Expression, name: str, dialect: str) -> dict[str, str]:
    """`node` as the dialect's users may write it, by way of writing: each SQL
    that sqlassert parses back to the same node type (SQLGlot writes DuckDB's
    `concat()` as `||`, which treats NULL differently; MySQL writes integer
    division `a DIV b`)."""
    arguments = [value for value in node.args.values() if isinstance(value, exp.Expression)]
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
    if not meaning.enum_error_value:
        table = "sqlassert_probe_enum"
        check = Check(
            _query(f"SELECT CASE WHEN count(*) = 0 THEN 1 ELSE 0 END FROM {table} WHERE a NOT IN ('x', 'y ')", dialect), "true",
            guard=(_table(dialect, table, "a ENUM('x', 'y ')"),),
            attempts=tuple(f"INSERT INTO {table} (a) VALUES ('{value}')" for value in ("z", "", "y")),
            tables=(table,),
        )
        yield Fact("enum_error_value=False", "enum_error_value=True", (check,))
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
            return ("PASS" if check.expect != "true" else "FAIL"), f"{check.query} raised {_first_line(error)}"
        value = rows[0][0] if rows and rows[0] else None
        truthy = value in (1, True) or value == "1"
        return ("PASS" if check.expect != "error" and truthy else "FAIL"), f"{check.query} returned {value!r}"
    finally:
        _drop(cursor, check.tables)


def _first_line(error: BaseException) -> str:
    return (str(error).strip().splitlines() or [type(error).__name__])[0][:160]


def run(cursor: Any, dialect: str, probed: Sequence[Fact]) -> list[Result]:
    """Each fact's outcome under the engine's defaults, then again under `PERMISSIVE` settings."""
    sessions = [()] + ([PERMISSIVE[dialect]] if dialect in PERMISSIVE else [])
    worst: dict[str, Result] = {}
    rank = {"PASS": 0, "VACUOUS": 1, "ERROR": 2, "FAIL": 3}
    for settings in sessions:
        for statement in settings:
            _execute(cursor, statement)
        for fact in probed:
            result = _run_fact(cursor, fact, settings)
            previous = worst.get(fact.name)
            if previous is None or rank[result.outcome] > rank[previous.outcome]:
                worst[fact.name] = result
    return [worst[fact.name] for fact in probed]


def _run_fact(cursor: Any, fact: Fact, settings: tuple[str, ...]) -> Result:
    """FAIL if a check fails, ERROR if one cannot run, PASS if one ran and
    every one held, VACUOUS if none applied."""
    under = f" (after {'; '.join(settings)})" if settings else ""
    vacuous: list[str] = []
    for check in fact.checks:
        try:
            outcome, detail = _check(cursor, check)
        except _ProbeError as error:
            return Result(fact, "ERROR", f"{error}{under}")
        if outcome == "FAIL":
            return Result(fact, "FAIL", f"{detail}{under}")
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
    """Add `version` to each passing fact's engine provenance (replacing a doc
    one) and remove it from each failing fact's; drop facts the dialect no
    longer has. An ERROR changes nothing. Returns what changed."""
    entries = provenance.setdefault(dialect, {})
    changes: list[str] = []
    current = {result.fact.name for result in results}
    for name in sorted(set(entries) - current):
        del entries[name]
        changes.append(f"dropped {name}: no longer a fact")
    for result in results:
        name, entry = result.fact.name, entries.get(result.fact.name)
        versions = list(entry.get("engine", ())) if entry else []
        if result.outcome in ("PASS", "VACUOUS") and version not in versions:
            entries[name] = {"engine": sorted([*versions, version])}
            changes.append(f"{name}: certified on {version}")
        elif result.outcome == "FAIL" and version in versions:
            versions.remove(version)
            if versions:
                entries[name] = {"engine": versions}
            else:
                del entries[name]
            changes.append(f"{name}: no longer certified on {version}")
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
            print(f"STRONGER {result.fact.name}: holds on {version} too (not claimed: a setting or another version may break it)")
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
