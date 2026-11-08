"""What each SQL dialect means by the constructs the analysis depends on.

One `DialectSemantics` per dialect name (SQLGlot's), read by `ir/convert.py`
while it lowers SQL: which Builtins raise an error rather than return NULL,
which text compares by identity, what `||` and `concat()` do with NULL, and
which constraints the database enforces. A dialect not listed gets
`DialectSemantics()`, whose arithmetic, collation and `||` are the cautious
choice; only its keys are taken as enforced, as before dialects were listed.

Every flag is a claim that holds under all of that dialect's settings, since
the SQL alone does not say which are in force (ADR 0005): MySQL's `||` is OR
or concatenation depending on `sql_mode`, so it is neither; Spark's
arithmetic raises or returns NULL depending on ANSI mode, so it may return
NULL. `docs/dialects.md` argues each entry and says how soundness is judged
without DuckDB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# How a dialect treats a declared key (PRIMARY KEY, UNIQUE) or foreign key:
# enforced; informational, trusted when marked `RELY`; or informational only.
type KeyTrust = Literal["enforced", "rely", "unenforced"]

# What `a || b` is: concatenation whose NULL operand makes it NULL ("strict"),
# concatenation that may skip a NULL operand ("lenient", Postgres arrays), or
# something whose meaning a setting decides ("ambiguous", MySQL); None when
# it is not modelled.
type PipesMeaning = Literal["strict", "lenient", "ambiguous"] | None

# What `concat(a, ...)` does with NULL: makes the result NULL ("strict"), or
# skips it, so the result is never NULL ("skips-null"); None when not modelled.
type ConcatMeaning = Literal["strict", "skips-null"] | None


@dataclass(frozen=True)
class DialectSemantics:
    # `+`, `-`, `*`, unary `-` and CAST raise an error on overflow or a bad
    # value rather than wrapping, saturating, or returning NULL.
    checked_arithmetic: bool = False
    # `/`, integer division and `%` by zero raise an error rather than return NULL.
    checked_division: bool = False
    # An implicit cast that fails raises an error, so a comparison, LIKE,
    # BETWEEN, NOT or bitwise operator of non-null operands is never NULL.
    casts_raise: bool = True
    # VARCHAR and TEXT with no COLLATE compare by identity, whatever the
    # database's or session's settings.
    binary_default_collation: bool = False
    # Collations (lower-cased) under which equal text is identical text.
    binary_collations: frozenset[str] = frozenset()
    # A DECIMAL declared without a scale keeps each value's own scale, so
    # equal values (1.0, 1.00) may differ: neither identity nor exact.
    unscaled_decimal: bool = False
    pipes: PipesMeaning = None
    concat: ConcatMeaning = None
    unique_keys: KeyTrust = "enforced"
    foreign_keys: KeyTrust = "enforced"
    # A PRIMARY KEY column is NOT NULL.
    primary_key_not_null: bool = True
    # A column-level `REFERENCES` is enforced (MySQL before 9.0 parses and ignores it).
    inline_references: bool = True
    # The table ENGINEs that enforce foreign keys (lower-cased), None for every table.
    foreign_key_engines: frozenset[str] | None = None
    # An ENUM column holds '' for a value outside its members (MySQL, outside
    # strict mode or under INSERT IGNORE), and its members lose trailing spaces.
    enum_error_value: bool = False
    # A zero-length string is NULL (Oracle): the literal '' is NULL.
    empty_string_is_null: bool = False

    @property
    def rely(self) -> bool:
        """Whether the dialect marks an informational constraint trusted with `RELY`."""
        return "rely" in (self.unique_keys, self.foreign_keys)


_SPARK = DialectSemantics(
    # ANSI mode decides whether overflow, division by zero and a bad cast
    # raise or return NULL; it is on by default only in recent versions.
    casts_raise=False,
    pipes="strict",
    concat="strict",
    unique_keys="rely",
    foreign_keys="rely",
)

_SEMANTICS: dict[str, DialectSemantics] = {
    "duckdb": DialectSemantics(
        checked_arithmetic=True,
        binary_default_collation=True,
        pipes="strict",
        concat="skips-null",
    ),
    "postgres": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        binary_default_collation=True,
        # Deterministic collations, which compare equal only identical strings.
        binary_collations=frozenset({"c", "posix", "ucs_basic", "default", "pg_c_utf8"}),
        unscaled_decimal=True,
        pipes="lenient",  # `ARRAY[1] || NULL` is `{1}`.
        concat="skips-null",
    ),
    "mysql": DialectSemantics(
        # Out of strict mode, a bad CAST or a bad date is NULL with a warning,
        # and division by zero is NULL in any mode.
        casts_raise=False,
        # The default collations ignore case (and accents, and some trailing spaces).
        binary_collations=frozenset({"utf8mb4_0900_bin", "binary"}),
        pipes="ambiguous",
        concat="strict",
        inline_references=False,
        foreign_key_engines=frozenset({"innodb", "ndb", "ndbcluster"}),
        enum_error_value=True,
    ),
    "snowflake": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        pipes="strict",
        concat="strict",
        unique_keys="rely",
        foreign_keys="rely",
    ),
    "bigquery": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        pipes="strict",
        concat="strict",
        unique_keys="unenforced",
        foreign_keys="unenforced",
    ),
    "redshift": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        pipes="strict",
        unique_keys="unenforced",
        foreign_keys="unenforced",
    ),
    "databricks": _SPARK,
    "spark": _SPARK,
    "spark2": _SPARK,
    "hive": DialectSemantics(casts_raise=False, pipes="strict", concat="strict", unique_keys="unenforced", foreign_keys="unenforced"),
    "sqlite": DialectSemantics(
        pipes="strict",
        # Foreign keys are checked only under `PRAGMA foreign_keys = ON`, off by default.
        foreign_keys="unenforced",
        # Only an INTEGER PRIMARY KEY (the rowid) is never NULL.
        primary_key_not_null=False,
    ),
    # A ClickHouse primary key orders and indexes a table; it never rejects a duplicate.
    "clickhouse": DialectSemantics(unique_keys="unenforced", foreign_keys="unenforced"),
    "oracle": DialectSemantics(empty_string_is_null=True),
}


# The dialects whose semantics are argued here (docs/dialects.md).
MODELLED_DIALECTS = frozenset(_SEMANTICS)


def semantics(dialect: str) -> DialectSemantics:
    """The semantics of a SQLGlot dialect name; cautious defaults for one not listed."""
    return _SEMANTICS.get(dialect.lower(), DialectSemantics())
