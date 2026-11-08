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
without DuckDB; `docs/dialect-provenance.json` records, per fact, the engine
versions it was certified on or the vendor doc it rests on
(`scripts/certify_dialects.py`).
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
    # VARCHAR and TEXT with no COLLATE compare by identity under the engine's
    # shipped default collation. A setting that changes that default (DuckDB's
    # `default_collation`) is out of scope; text with any COLLATE is never
    # taken to compare by identity (docs/dialects.md).
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
    zoned_timestamp_is_instant=True,
    pipes="strict",
    concat="strict",
    concat_ws="skips-null",
    unique_keys="rely",
    foreign_keys="rely",
)

_SEMANTICS: dict[str, DialectSemantics] = {
    "duckdb": DialectSemantics(
        checked_arithmetic=True,
        text_is_binary=True,
        zoned_timestamp_is_instant=True,
        pipes="strict",
        concat="skips-null",
        concat_ws="skips-null",
    ),
    "postgres": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        text_is_binary=True,
        unscaled_decimal=True,
        zoned_timestamp_is_instant=True,
        pipes="lenient",  # `ARRAY[1] || NULL` is `{1}`.
        concat="skips-null",
        concat_ws="skips-null",
    ),
    "mysql": DialectSemantics(
        # Out of strict mode, a bad CAST or a bad date is NULL with a warning,
        # and division by zero is NULL in any mode.
        casts_raise=False,
        # Not `text_is_binary`: the default collations ignore case (and
        # accents, and some trailing spaces).
        zoned_timestamp_is_instant=True,  # TIMESTAMP is converted to UTC.
        pipes="ambiguous",
        concat="strict",
        concat_ws="skips-null",
        inline_references=False,
        foreign_key_engines=frozenset({"innodb", "ndb", "ndbcluster"}),
        enum_error_value=True,
    ),
    "snowflake": DialectSemantics(
        # Not `checked_arithmetic`: its docs never say integer overflow raises.
        checked_division=True,
        pipes="strict",
        concat="strict",
        concat_ws="strict",  # `concat_ws(',', 'a', NULL)` is NULL.
        unique_keys="rely",
        foreign_keys="rely",
    ),
    "bigquery": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        zoned_timestamp_is_instant=True,
        pipes="strict",
        concat="strict",
        unique_keys="unenforced",
        foreign_keys="unenforced",
    ),
    "redshift": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        zoned_timestamp_is_instant=True,
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
        concat_ws="skips-null",
        # Foreign keys are checked only under `PRAGMA foreign_keys = ON`, off by default.
        foreign_keys="unenforced",
        # Only an INTEGER PRIMARY KEY (the rowid) is never NULL.
        primary_key_not_null=False,
    ),
    # A ClickHouse primary key orders and indexes a table; it never rejects a duplicate.
    "clickhouse": DialectSemantics(concat_ws="strict", unique_keys="unenforced", foreign_keys="unenforced"),
    "oracle": DialectSemantics(empty_string_is_null=True),
    # Trino has no constraint but NOT NULL: its CREATE TABLE rejects a key, so
    # one in the SQL describes a table some other engine holds.
    "trino": DialectSemantics(
        checked_arithmetic=True,
        checked_division=True,
        # VARCHAR compares by code point and Trino has no collations.
        text_is_binary=True,
        # Not `zoned_timestamp_is_instant`: a TIMESTAMP WITH TIME ZONE keeps
        # its zone, and `CAST(ts AS VARCHAR)` writes it.
        # Unlike Postgres, a NULL array or element operand makes `||` NULL too.
        pipes="strict",
        concat="strict",
        # Not modelled: it skips a NULL VARCHAR, but its ARRAY(VARCHAR)
        # overload is NULL for a NULL array, and SQLGlot parses both alike.
        unique_keys="unenforced",
        foreign_keys="unenforced",
    ),
}


# The dialects whose semantics are argued here (docs/dialects.md).
MODELLED_DIALECTS = frozenset(_SEMANTICS)


def semantics(dialect: str) -> DialectSemantics:
    """The semantics of a SQLGlot dialect name; cautious defaults for one not listed."""
    return _SEMANTICS.get(dialect.lower(), DialectSemantics())
