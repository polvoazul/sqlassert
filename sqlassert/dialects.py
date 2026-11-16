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
    # The type each function returns whatever its arguments' types, by the
    # SQLGlot node it parses to: a select item that is such a call has that
    # type, as a column declared with it would. Only where every argument type
    # the engine accepts gives the same result type (`date_trunc` keeps its
    # input's type, so it is not here). Certified with the engine's own type
    # function (`scripts/certify_dialects.py`); a dialect without one claims none.
    result_types: tuple[tuple[str, str], ...] = ()
    # Hash functions modelled as strict and total, by SQLGlot node: NULL
    # exactly when the argument is. SQLGlot parses a hex-text hash as `MD5`,
    # `SHA` (sha1) or `SHA2` and a raw-bytes one as `MD5Digest`, `SHA1Digest`
    # or `SHA2Digest`, though ClickHouse's `SHA1`/`SHA256` (bytes) parse as
    # `SHA`/`SHA2`. None is standard SQL, so each dialect claims its own.
    # `sha2(x, n)` where `n` is free is NULL for an unsupported `n` in MySQL,
    # Spark and Hive, so `SHA2` is claimed only where the length is fixed.
    hash_functions: tuple[str, ...] = ()
    # The type a VALUES column has when every non-NULL value in it is a
    # literal of one kind (NULL does not change it), by kind, as the engine
    # names the type (`values_type(KIND)=TYPE`): "string"; "integer", each
    # value within INTEGER's range; "bigint", each within BIGINT's and one
    # beyond INTEGER's (the column has its widest row's type); "decimal", each
    # written `d.f` with a whole part that does not start with 0, a type
    # DECIMAL(p, s) of the widest whole part and scale. A kind not listed, a
    # column of mixed kinds, of any other literal (a boolean, an exponent, a
    # whole part 0, an integer beyond BIGINT) and of any expression stay untyped.
    # A string type claims only that it is text: whether it compares by
    # identity is `text_is_binary`, as for a declared column.
    values_types: tuple[tuple[str, str], ...] = ()
    # The columns, in order, that `SELECT * FROM f(...)` returns for a table
    # function (or UNNEST, as `unnest`) of one argument list, by lower-case
    # name (`table_function(NAME)=COLUMNS`): whatever its arguments, and
    # whether or not the alias lists column names, which rename the leading
    # columns. A function with one column and an alias with no column list
    # names it by the alias, as DuckDB does (`range(3) AS r` is the column
    # `r`). A function not listed, an UNNEST of several arguments or WITH
    # ORDINALITY has columns not known without the data or the engine.
    table_functions: tuple[tuple[str, tuple[str, ...]], ...] = ()

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
    # Tentative. Not SHA2: `sha2(x, n)` is NULL for an unsupported `n`.
    result_types=(("MD5", "STRING"), ("SHA", "STRING")),
    hash_functions=("MD5", "SHA"),
    values_types=(("string", "string"), ("integer", "int"), ("bigint", "bigint"), ("decimal", "decimal")),
)

# What `json_each` and `json_tree` return, in DuckDB and SQLite alike.
_JSON_TABLE_COLUMNS = ("key", "value", "type", "atom", "id", "parent", "fullkey", "path")

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
        result_types=(
            *((node, "VARCHAR") for node in (
                "MD5", "SHA", "SHA2", "Lower", "Upper", "Trim", "Replace", "Left", "Right", "Substring", "ConcatWs", "Dayname", "Monthname",
            )),
            *((node, "BIGINT") for node in ("Length", "StrPosition", "Year", "Month", "Day")),
        ),
        hash_functions=("MD5", "SHA", "SHA2"),
        values_types=(("string", "VARCHAR"), ("integer", "INTEGER"), ("bigint", "BIGINT"), ("decimal", "DECIMAL")),
        table_functions=(
            ("json_each", _JSON_TABLE_COLUMNS), ("json_tree", _JSON_TABLE_COLUMNS),
            ("range", ("range",)), ("generate_series", ("generate_series",)), ("unnest", ("unnest",)),
        ),
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
        # `sha256(x)` returns bytea: SHA2Digest. No Postgres SQL parses to SHA or SHA2.
        result_types=(("MD5", "TEXT"), ("SHA2Digest", "BYTEA")),
        hash_functions=("MD5", "SHA2Digest"),
        # A numeric literal with a point is an unscaled NUMERIC: no decimal is claimed.
        values_types=(("string", "text"), ("integer", "integer"), ("bigint", "bigint")),
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
        # No type function to certify result types with. Not SHA2: its length is free.
        hash_functions=("MD5", "SHA"),
        # Every integer literal is a BIGINT. MySQL spells a row `ROW(...)`, which is not lowered.
        values_types=(("string", "varchar"), ("integer", "bigint"), ("bigint", "bigint"), ("decimal", "decimal")),
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
        # Tentative (docs/dialect-provenance.json). `md5_binary` is MD5Digest.
        result_types=(("MD5", "VARCHAR"), ("SHA", "VARCHAR"), ("MD5Digest", "BINARY")),
        hash_functions=("MD5", "SHA", "MD5Digest"),
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
        # Tentative. `md5` and `sha256` return BYTES; `to_hex(md5(x))` parses as MD5.
        result_types=(("MD5", "STRING"), ("MD5Digest", "BYTES"), ("SHA1Digest", "BYTES"), ("SHA2Digest", "BYTES")),
        hash_functions=("MD5", "MD5Digest", "SHA1Digest", "SHA2Digest"),
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
        # Tentative.
        result_types=(("MD5", "VARCHAR"), ("SHA", "VARCHAR")),
        hash_functions=("MD5", "SHA"),
    ),
    "databricks": replace(
        _SPARK, checked_division=True, casts_raise=True,
        # Certified on Apache Spark only; Databricks's docs do not say a sum meets NaN as a value.
        checked_sum=False, greatest=None, subscript_strict=False,
    ),
    "spark": _SPARK,
    "hive": DialectSemantics(casts_raise=False, pipes="strict", concat="strict", unique_keys="unenforced", foreign_keys="unenforced",
        check_constraints="unenforced",
        # Tentative. Not SHA2: `sha2(x, n)` is NULL for an unsupported `n`.
        result_types=(("MD5", "STRING"), ("SHA", "STRING")),
        hash_functions=("MD5", "SHA"),
    ),
    "sqlite": DialectSemantics(
        pipes="strict",
        concat_ws="skips-null",
        # Foreign keys are checked only under `PRAGMA foreign_keys = ON`, off by default.
        foreign_keys="unenforced",
        # Only an INTEGER PRIMARY KEY (the rowid) is never NULL.
        primary_key_not_null=False,
        table_functions=(("json_each", _JSON_TABLE_COLUMNS), ("json_tree", _JSON_TABLE_COLUMNS)),
    ),
    # A ClickHouse primary key orders and indexes a table; it never rejects a duplicate.
    # Its `MD5`/`SHA1`/`SHA256` return FixedString bytes, wrapped in Nullable
    # for a Nullable argument: a type that follows the argument, so no result
    # type is claimed.
    "clickhouse": DialectSemantics(
        concat_ws="strict", unique_keys="unenforced", foreign_keys="unenforced", hash_functions=("MD5Digest", "SHA", "SHA2"),
    ),
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
        # `md5`/`sha1`/`sha256` take and return VARBINARY.
        result_types=(("MD5Digest", "VARBINARY"), ("SHA", "VARBINARY"), ("SHA2Digest", "VARBINARY")),
        hash_functions=("MD5Digest", "SHA", "SHA2Digest"),
        # A string is `varchar(n)` for its longest value.
        values_types=(("string", "varchar"), ("integer", "integer"), ("bigint", "bigint"), ("decimal", "decimal")),
    ),
}


# The dialects whose semantics are argued here (docs/dialects.md).
MODELLED_DIALECTS = frozenset(_SEMANTICS)


def semantics(dialect: str) -> DialectSemantics:
    """The semantics of a SQLGlot dialect name; cautious defaults for one not listed."""
    return _SEMANTICS.get(dialect.lower(), DialectSemantics())
