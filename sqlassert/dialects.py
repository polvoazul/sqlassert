"""What each SQL dialect means by the constructs the analysis depends on.

One `DialectSemantics` per dialect name (SQLGlot's), read by `ir/convert.py`
while it lowers SQL: which Builtins raise an error rather than return NULL,
which text compares by identity, what `||`, `concat()`, `greatest()` and
subscripts do with NULL, and which constraints the database enforces. A dialect not listed gets
`DialectSemantics()`, whose arithmetic, collation and `||` are the cautious
choice; only its keys are taken as enforced, as before dialects were listed.

Every flag is a claim about the dialect's latest release under its default
settings, strict modes on (ADR 0006): MySQL's `||` is OR, since the default
`sql_mode` has no PIPES_AS_CONCAT; Spark is modelled with ANSI mode on. `docs/dialects.md` argues each entry and says how soundness is judged
without DuckDB; `docs/dialect-provenance.json` records, per fact, the engine
versions it was certified on or the vendor doc it rests on
(`scripts/certify_dialects.py`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

# How a dialect treats a declared key (PRIMARY KEY, UNIQUE) or foreign key:
# enforced; informational, trusted when marked `RELY`; or informational only.
type KeyTrust = Literal["enforced", "rely", "unenforced"]

# What `a || b` is: concatenation whose NULL operand makes it NULL ("strict"),
# or concatenation that may skip a NULL operand ("lenient", Postgres arrays);
# None when it is not modelled (MySQL, where it is OR).
type PipesMeaning = Literal["strict", "lenient"] | None

# What `concat(a, ...)` does with NULL: makes the result NULL ("strict"), or
# skips it, so the result is never NULL ("skips-null"); None when not modelled.
type ConcatMeaning = Literal["strict", "skips-null"] | None

# What `greatest(a, ...)` and `least(a, ...)` do with NULL: make the result
# NULL ("strict"), or skip it, so the result is NULL only when every argument
# is ("skips-null"); None when not modelled.
type GreatestMeaning = Literal["strict", "skips-null"] | None


@dataclass(frozen=True)
class DialectSemantics:
    # `+`, `-`, `*`, unary `-` and CAST raise an error on overflow or a bad
    # value rather than wrapping, saturating, or returning NULL.
    checked_arithmetic: bool = False
    # `/`, integer division and `%` by zero raise an error rather than return NULL.
    checked_division: bool = False
    # `sum` and `avg` of non-null values are never NULL: an overflow raises
    # (or widens the type) rather than returning NULL, and NaN or infinity is
    # a value, not NULL (SQLite turns NaN into NULL).
    checked_sum: bool = False
    # An implicit cast that fails raises an error, so a comparison, LIKE,
    # BETWEEN, NOT or bitwise operator of non-null operands is never NULL.
    casts_raise: bool = True
    # VARCHAR and TEXT compare by identity under the engine's shipped default
    # collation. A setting that changes that default (DuckDB's
    # `default_collation`) is out of scope, and a COLLATE is unsupported: it
    # is opaque, and a column declared with one is untyped (docs/dialects.md).
    text_is_binary: bool = False
    # A DECIMAL declared without a scale keeps each value's own scale, so
    # equal values (1.0, 1.00) may differ: neither identity nor exact.
    unscaled_decimal: bool = False
    # A TIMESTAMP WITH TIME ZONE (SQLGlot's TIMESTAMPTZ: MySQL's, BigQuery's
    # and Spark's TIMESTAMP too) holds an instant only, so equal values are
    # identical. Not where it keeps the zone it was given (Trino, Snowflake's
    # TIMESTAMP_TZ, Oracle): equal instants in two zones differ as text.
    zoned_timestamp_is_instant: bool = False
    pipes: PipesMeaning = None
    concat: ConcatMeaning = None
    # What `concat_ws(sep, a, ...)` does with a NULL value (a NULL separator
    # makes it NULL either way); None where it does not exist or is not modelled.
    concat_ws: ConcatMeaning = None
    greatest: GreatestMeaning = None
    # `xs[i]` (an element of a list, array, map or struct) is NULL when `xs` is.
    subscript_strict: bool = False
    unique_keys: KeyTrust = "enforced"
    foreign_keys: KeyTrust = "enforced"
    # CHECK constraints. Where the dialect has none (Snowflake, BigQuery,
    # Redshift, Trino), one in the SQL describes a table another engine holds.
    check_constraints: KeyTrust = "enforced"
    # A PRIMARY KEY column is NOT NULL.
    primary_key_not_null: bool = True
    # A column-level `REFERENCES` is enforced (MySQL before 9.0 parses and ignores it).
    inline_references: bool = True
    # The table ENGINEs that enforce foreign keys (lower-cased), None for every table.
    foreign_key_engines: frozenset[str] | None = None
    # An ENUM's members lose their trailing spaces (MySQL). An ENUM holds only
    # its members: MySQL's '' for any other value, outside strict mode or
    # under INSERT IGNORE, is out of scope (docs/dialects.md).
    enum_strips_trailing_spaces: bool = False
    # A zero-length string is NULL (Oracle): the literal '' is NULL.
    empty_string_is_null: bool = False
    # `EXCEPT ALL` and `INTERSECT ALL` exist. Only gates suggesting them, so
    # it is no soundness claim and is not certified: a wrong True suggests SQL
    # the engine rejects, never a wrong proof.
    set_operations_all: bool = False
    # An integer or decimal compared with a float is compared exactly (SQLite,
    # ClickHouse), not converted to the float type first. Only gates the
    # lossy-comparison hazard, so it is no soundness claim and is not
    # certified: a wrong value misses or adds a hazard, never a wrong proof.
    exact_float_comparison: bool = False

    @property
    def rely(self) -> bool:
        """Whether the dialect marks an informational constraint trusted with `RELY`."""
        return "rely" in (self.unique_keys, self.foreign_keys)


# Spark 4 and Databricks under ANSI mode, their default (ADR 0006): overflow
# and a bad CAST raise. Apache Spark's docs say `div` by zero is NULL and do
# not say a failed implicit cast in a comparison raises; Databricks's say
# both raise (docs/dialect-provenance.json).
_SPARK = DialectSemantics(
    checked_arithmetic=True,
    casts_raise=False,
    zoned_timestamp_is_instant=True,
    pipes="strict",
    concat="strict",
    concat_ws="skips-null",
    # Certified on Spark 4.3, not Databricks (`databricks` below drops them).
    checked_sum=True,
    greatest="skips-null",
    subscript_strict=True,
    unique_keys="rely",
    foreign_keys="rely",
    # Delta tables check them, other tables cannot declare them.
    check_constraints="unenforced",
    set_operations_all=True,
)

_SEMANTICS: dict[str, DialectSemantics] = {
    "duckdb": DialectSemantics(
        checked_arithmetic=True,
        checked_sum=True,
        text_is_binary=True,
        zoned_timestamp_is_instant=True,
        pipes="strict",
        concat="skips-null",
        concat_ws="skips-null",
        greatest="skips-null",
        subscript_strict=True,
        set_operations_all=True,
    ),
    "postgres": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        checked_sum=True,
        text_is_binary=True,
        unscaled_decimal=True,
        zoned_timestamp_is_instant=True,
        pipes="lenient",  # `ARRAY[1] || NULL` is `{1}`.
        concat="skips-null",
        concat_ws="skips-null",
        greatest="skips-null",
        subscript_strict=True,
        set_operations_all=True,
    ),
    "mysql": DialectSemantics(
        # MySQL under its default strict sql_mode only (docs/dialects.md), so
        # a comparison of non-null operands is non-null. Not `checked_arithmetic`:
        # `-x` of the least BIGINT is an exact DECIMAL rather than an error, and
        # division by zero is NULL in any mode.
        # `sum` of integers is an exact DECIMAL, and a DOUBLE holds no
        # infinity or NaN (a DOUBLE sum that overflows is 0).
        checked_sum=True,
        # Not `text_is_binary`: the default collations ignore case (and
        # accents, and some trailing spaces).
        zoned_timestamp_is_instant=True,  # TIMESTAMP is converted to UTC.
        concat="strict",
        concat_ws="skips-null",
        greatest="strict",
        # Not `subscript_strict`: MySQL has no `xs[i]`.
        inline_references=False,
        foreign_key_engines=frozenset({"innodb", "ndb", "ndbcluster"}),
        enum_strips_trailing_spaces=True,
        set_operations_all=True,  # Since 8.0.31.
    ),
    "snowflake": DialectSemantics(
        # Not `checked_arithmetic`: its docs never say integer overflow raises.
        checked_division=True,
        pipes="strict",
        concat="strict",
        concat_ws="strict",  # `concat_ws(',', 'a', NULL)` is NULL.
        unique_keys="rely",
        foreign_keys="rely",
        check_constraints="unenforced",
    ),
    "bigquery": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        # Its docs: overflow raises, and NaN or an infinity makes NaN or an infinity.
        checked_sum=True,
        zoned_timestamp_is_instant=True,
        pipes="strict",
        concat="strict",
        unique_keys="unenforced",
        foreign_keys="unenforced",
        check_constraints="unenforced",
    ),
    "redshift": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        # Not `checked_sum`: its docs say nothing of a sum that overflows or meets NaN.
        zoned_timestamp_is_instant=True,
        pipes="strict",
        unique_keys="unenforced",
        foreign_keys="unenforced",
        check_constraints="unenforced",
    ),
    "databricks": replace(
        _SPARK, checked_division=True, casts_raise=True,
        # Certified on Apache Spark only; Databricks's docs do not say a sum meets NaN as a value.
        checked_sum=False, greatest=None, subscript_strict=False,
    ),
    "spark": _SPARK,
    "hive": DialectSemantics(casts_raise=False, pipes="strict", concat="strict", unique_keys="unenforced", foreign_keys="unenforced",
        check_constraints="unenforced",
    ),
    "sqlite": DialectSemantics(
        pipes="strict",
        concat_ws="skips-null",
        # Foreign keys are checked only under `PRAGMA foreign_keys = ON`, off by default.
        foreign_keys="unenforced",
        # Only an INTEGER PRIMARY KEY (the rowid) is never NULL.
        primary_key_not_null=False,
        exact_float_comparison=True,  # 9007199254740993 = 9007199254740992.0 is false.
    ),
    # A ClickHouse primary key orders and indexes a table; it never rejects a duplicate.
    "clickhouse": DialectSemantics(concat_ws="strict", unique_keys="unenforced", foreign_keys="unenforced", exact_float_comparison=True),
    "oracle": DialectSemantics(empty_string_is_null=True),
    # Trino has no constraint but NOT NULL: its CREATE TABLE rejects a key, so
    # one in the SQL describes a table some other engine holds.
    "trino": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        checked_sum=True,
        # VARCHAR compares by code point and Trino has no collations.
        text_is_binary=True,
        # Not `zoned_timestamp_is_instant`: a TIMESTAMP WITH TIME ZONE keeps
        # its zone, and `CAST(ts AS VARCHAR)` writes it.
        # Unlike Postgres, a NULL array or element operand makes `||` NULL too.
        pipes="strict",
        concat="strict",
        greatest="strict",
        # NULL for a NULL array or map; an index out of range raises.
        subscript_strict=True,
        # Not modelled: it skips a NULL VARCHAR, but its ARRAY(VARCHAR)
        # overload is NULL for a NULL array, and SQLGlot parses both alike.
        unique_keys="unenforced",
        foreign_keys="unenforced",
        check_constraints="unenforced",
        set_operations_all=True,
    ),
}


# The dialects whose semantics are argued here (docs/dialects.md).
MODELLED_DIALECTS = frozenset(_SEMANTICS)


def semantics(dialect: str) -> DialectSemantics:
    """The semantics of a SQLGlot dialect name; cautious defaults for one not listed."""
    return _SEMANTICS.get(dialect.lower(), DialectSemantics())
