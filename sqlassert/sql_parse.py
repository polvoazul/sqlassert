"""Parse SQL text into a SQL Program: Create Statements plus at most one Root
Select, with join markers resolved onto the joins they mark, column markers
resolved onto the Select Expressions they follow, and case markers resolved
onto the simple `CASE` expressions they precede.

The marker is real syntax, not a comment we go looking for: a custom dialect
tokenizes `/**ASSERT UNIQUE**/` as a token of its own and the join rule consumes it, so
the grammar is what requires a marker to sit immediately before its join. The
line is then recorded on the `Join` node, and nothing downstream re-reads SQL
text. `/**ASSERT EXHAUSTIVE**/` is likewise a primary expression of its own
that must continue with `CASE`. SQLGlot objects stop at this boundary; only the IR parser reads them.
"""

from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple
import enum
import re

import sqlglot
from sqlglot import exp
from sqlglot import tokens as sqlglot_tokens
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.tokens import Token, TokenType

from sqlassert import diagnostics as diag
from sqlassert.diagnostics import Diagnostic
from sqlassert.ir.model import LiteralValue
from sqlassert.provenance import SQL, Origin

# Where a resolved assertion is recorded on its Join node, and which join marker it was.
_ASSERTED_AT = "sqlassert.asserted_at"
_JOIN_MARKER_KIND = "sqlassert.join_marker_kind"

# A comment opening with `/**` is how an author writes an assertion or a
# Property Declaration.
_MARKER_SHAPED = re.compile(r"/\*\*.*?\*/", re.DOTALL)


class SqlassertToken(enum.Enum):
    """Token types sqlassert adds to a dialect.

    Deliberately a plain Enum: SQLGlot's own `TokenType` is an `IntEnum`, so a
    member of ours sharing its integer value would compare equal to a real token
    type and be swallowed by SQLGlot's keyword sets. A plain member can equal
    nothing but itself, which also means no rule but ours can consume it.
    """

    SQLASSERT_UNIQUE = enum.auto()
    SQLASSERT_EXACTLY_ONE = enum.auto()
    SQLASSERT_EXHAUSTIVE = enum.auto()
    SQLASSERT_UNIQUE_SET_OPEN = enum.auto()
    SQLASSERT_NON_NULL_UNIQUE_SET_OPEN = enum.auto()
    SQLASSERT_DECLARED_UNIQUE_SET_OPEN = enum.auto()
    SQLASSERT_DECLARED_NON_NULL_UNIQUE_SET_OPEN = enum.auto()
    SQLASSERT_NOT_NULL_OPEN = enum.auto()
    SQLASSERT_DECLARED_NOT_NULL_OPEN = enum.auto()
    SQLASSERT_FOREIGN_KEY_OPEN = enum.auto()
    SQLASSERT_DECLARED_FOREIGN_KEY_OPEN = enum.auto()
    SQLASSERT_DETERMINES_OPEN = enum.auto()
    SQLASSERT_DECLARED_DETERMINES_OPEN = enum.auto()
    SQLASSERT_ENUM_OPEN = enum.auto()
    SQLASSERT_DECLARED_ENUM_OPEN = enum.auto()
    SQLASSERT_MARKER_CLOSE = enum.auto()


# Every marker sqlassert understands, and the token each becomes. The single
# source of truth for the join markers and the fixed opening/closing
# literals of a column marker: the dialect tokenizes exactly these, and
# `_unrecognized_markers` reports anything else shaped like one.
#
# `ASSERT` asks for a proof; `DECLARE` states a trusted Property Declaration.
# A column marker -- `/**ASSERT UNIQUE(col, ...)**/`, `/**ASSERT NOT NULL(...)**/`,
# `/**DECLARE NOT NULL UNIQUE(...)**/`, and the rest of `_COLUMN_MARKER_OPENS` --
# carries a variable column list, so unlike the join marker it cannot be one
# fixed `KEYWORDS` literal: only its open and close are fixed text; the column
# list between them is ordinary SQL, parsed with the parser's own
# identifier-list machinery once the open token is matched
# (`_Parser._parse_select_query` below). A Foreign Key marker continues its
# column list with `REFERENCES relation(col, ...)`, pairing columns by position:
# `/**ASSERT FOREIGN KEY(a, b) REFERENCES t(x, y)**/`. A Functional Dependency
# marker splits its list with `->`: `/**ASSERT DETERMINES(a, b -> c)**/`. An
# Enumerated Domain marker names one column and the literals its non-null
# values are drawn from: `/**ASSERT ENUM(status IN ('new', 'paid'))**/`.
# `/**ASSERT EXHAUSTIVE**/` is written inside an expression, immediately
# before the simple `CASE` it marks.
MARKERS = {
    "/**ASSERT UNIQUE**/": SqlassertToken.SQLASSERT_UNIQUE,
    "/**ASSERT EXACTLY ONE**/": SqlassertToken.SQLASSERT_EXACTLY_ONE,
    "/**ASSERT EXHAUSTIVE**/": SqlassertToken.SQLASSERT_EXHAUSTIVE,
    "/**ASSERT UNIQUE(": SqlassertToken.SQLASSERT_UNIQUE_SET_OPEN,
    "/**ASSERT NOT NULL UNIQUE(": SqlassertToken.SQLASSERT_NON_NULL_UNIQUE_SET_OPEN,
    # `PRIMARY KEY` is a convenience alias for `NOT NULL UNIQUE`: it does not
    # mean one key per relation, and a relation may carry several.
    "/**ASSERT PRIMARY KEY(": SqlassertToken.SQLASSERT_NON_NULL_UNIQUE_SET_OPEN,
    "/**ASSERT NOT NULL(": SqlassertToken.SQLASSERT_NOT_NULL_OPEN,
    "/**DECLARE UNIQUE(": SqlassertToken.SQLASSERT_DECLARED_UNIQUE_SET_OPEN,
    "/**DECLARE NOT NULL UNIQUE(": SqlassertToken.SQLASSERT_DECLARED_NON_NULL_UNIQUE_SET_OPEN,
    "/**DECLARE PRIMARY KEY(": SqlassertToken.SQLASSERT_DECLARED_NON_NULL_UNIQUE_SET_OPEN,
    "/**DECLARE NOT NULL(": SqlassertToken.SQLASSERT_DECLARED_NOT_NULL_OPEN,
    "/**ASSERT FOREIGN KEY(": SqlassertToken.SQLASSERT_FOREIGN_KEY_OPEN,
    "/**DECLARE FOREIGN KEY(": SqlassertToken.SQLASSERT_DECLARED_FOREIGN_KEY_OPEN,
    "/**ASSERT DETERMINES(": SqlassertToken.SQLASSERT_DETERMINES_OPEN,
    "/**DECLARE DETERMINES(": SqlassertToken.SQLASSERT_DECLARED_DETERMINES_OPEN,
    "/**ASSERT ENUM(": SqlassertToken.SQLASSERT_ENUM_OPEN,
    "/**DECLARE ENUM(": SqlassertToken.SQLASSERT_DECLARED_ENUM_OPEN,
    "**/": SqlassertToken.SQLASSERT_MARKER_CLOSE,
}

# The join markers, written immediately before a JOIN, and the kind each states:
# "unique-join" a Unique Join, "exactly-one-join" an Exactly One Join.
JOIN_MARKER_KINDS = {
    SqlassertToken.SQLASSERT_UNIQUE: "unique-join",
    SqlassertToken.SQLASSERT_EXACTLY_ONE: "exactly-one-join",
}

# How diagnostics name each join marker kind.
JOIN_MARKER_NOUNS = {"unique-join": "unique join", "exactly-one-join": "exactly one join"}

# The markers written as one fixed literal: the join markers and the case marker.
_FIXED_MARKER_TOKENS = frozenset({*JOIN_MARKER_KINDS, SqlassertToken.SQLASSERT_EXHAUSTIVE})

# Where a resolved case marker's line is recorded on its `Case` node.
_EXHAUSTIVE_AT = "sqlassert.exhaustive_at"

# The open literals of a column marker, and the kind each one states --
# "unique" is a nullable-tolerant Unique Set, "key" the stricter
# Non-Null Unique Set, "not-null" non-nullness of every listed column, and
# "foreign-key" a Foreign Key from the listed columns, "determines" a Functional
# Dependency from the columns before `->` to those after, "enum" an Enumerated
# Domain of the one column before `IN`. A "declared-" kind states
# the same Property as a trusted declaration.
_COLUMN_MARKER_OPENS = {
    "/**ASSERT UNIQUE(": "unique",
    "/**ASSERT NOT NULL UNIQUE(": "key",
    "/**ASSERT PRIMARY KEY(": "key",
    "/**ASSERT NOT NULL(": "not-null",
    "/**DECLARE UNIQUE(": "declared-unique",
    "/**DECLARE NOT NULL UNIQUE(": "declared-key",
    "/**DECLARE PRIMARY KEY(": "declared-key",
    "/**DECLARE NOT NULL(": "declared-not-null",
    "/**ASSERT FOREIGN KEY(": "foreign-key",
    "/**DECLARE FOREIGN KEY(": "declared-foreign-key",
    "/**ASSERT DETERMINES(": "determines",
    "/**DECLARE DETERMINES(": "declared-determines",
    "/**ASSERT ENUM(": "enum",
    "/**DECLARE ENUM(": "declared-enum",
}

# Column marker kinds that are Property Declarations rather than Assertions.
DECLARED_KINDS = frozenset({
    "declared-unique", "declared-key", "declared-not-null", "declared-foreign-key", "declared-determines",
    "declared-enum",
})

# Column marker kinds followed by `REFERENCES relation(col, ...)`.
FOREIGN_KEY_KINDS = frozenset({"foreign-key", "declared-foreign-key"})

# Column marker kinds whose list is `determinant -> dependent`.
DETERMINES_KINDS = frozenset({"determines", "declared-determines"})

# Column marker kinds whose list is `column IN (literal, ...)`.
ENUM_KINDS = frozenset({"enum", "declared-enum"})

# How diagnostics name each column marker kind.
MARKER_NOUNS = {
    "unique": "unique set assertion",
    "key": "not null unique assertion",
    "not-null": "not null assertion",
    "declared-unique": "unique set declaration",
    "declared-key": "not null unique declaration",
    "declared-not-null": "not null declaration",
    "foreign-key": "foreign key assertion",
    "declared-foreign-key": "foreign key declaration",
    "determines": "functional dependency assertion",
    "declared-determines": "functional dependency declaration",
    "enum": "enum assertion",
    "declared-enum": "enum declaration",
}

# Where resolved column markers are recorded on its Select node.
_COLUMN_MARKERS_AT = "sqlassert.unique_set_asserted_at"

# The source offset and line at which a marker for this Join, Select, or simple
# CASE would be written: a join marker right before the join's first keyword, a
# column marker right after the Select's last token, a case marker right before
# CASE. `suggest` pastes markers there.
_MARKER_POSITION = "sqlassert.marker_position"

_COLUMN_MARKER_OPEN_TOKENS = {
    MARKERS[open_]: kind for open_, kind in _COLUMN_MARKER_OPENS.items()
}


_INTEGER = re.compile(r"\d+")
_DECIMAL = re.compile(r"\d+\.\d*|\.\d+")
_EXPONENT = re.compile(r"(\d+\.?\d*|\.\d+)[eE][+-]?\d+")


def literal_value(expression: exp.Expression) -> LiteralValue | None:
    """The typed value of a string, numeric, or boolean literal, or None for
    any other expression. A numeric literal may be negated."""
    if isinstance(expression, exp.Boolean):
        return LiteralValue(type="boolean", text="TRUE" if expression.this else "FALSE")
    if isinstance(expression, exp.Literal) and expression.is_string:
        return LiteralValue(type="string", text=expression.this)
    negative = isinstance(expression, exp.Neg)
    number = expression.this if negative else expression
    if not isinstance(number, exp.Literal) or number.is_string:
        return None
    text: str = number.this
    if _INTEGER.fullmatch(text):
        # Leading zeros change neither the value nor the SQL type.
        text = str(int(text))
        negative = negative and text != "0"
    elif _EXPONENT.fullmatch(text):
        text = text.lower()
    elif not _DECIMAL.fullmatch(text):
        return None
    return LiteralValue(type="numeric", text="-" + text if negative else text)


class ColumnMarker(NamedTuple):
    """One column marker as written: `kind` is a value of `_COLUMN_MARKER_OPENS`.

    A Foreign Key marker also names the referenced relation, as written, and
    its referenced columns, paired with `columns` by position. A Functional
    Dependency marker's `columns` are its determinant and `dependent` the
    columns after `->`. An Enumerated Domain marker's one column is `columns`
    and its distinct typed literals, in the order written, are `values`.
    """

    kind: str
    columns: tuple[str, ...]
    line: int
    referenced_relation: str | None = None
    referenced_columns: tuple[str, ...] = ()
    dependent: tuple[str, ...] = ()
    values: tuple[LiteralValue, ...] = ()


class UnattachedMarker(ParseError):
    """A marker that does not mark a join."""

    def __init__(self, message: str, line: int, text: str) -> None:
        super().__init__(message)
        self.line = line
        self.text = text


@lru_cache(maxsize=None)
def sqlassert_dialect(base: str) -> type[Dialect]:
    """The named dialect, plus Property Markers as real syntax.

    Cached because SQLGlot registers every dialect class by name, so one class
    per base dialect must be built once rather than per analysis.
    """
    parent = type(Dialect.get_or_raise(base))

    # `Dialect` does not declare `Tokenizer`/`Parser` as class attributes --
    # every concrete dialect sets its own, so the base class's static type
    # cannot know they exist. This is exactly the private-API coupling
    # documented below, not a type worth narrowing further.
    #
    # `SQLASSERT_UNIQUE` is deliberately not a `TokenType` (see above). Before
    # SQLGlot 30, the optional Rust tokenizer indexed every `KEYWORDS` value
    # against `TokenType` while building the class and KeyErrored on ours. Those
    # versions expose a private switch which lets this dialect consistently use
    # the Python tokenizer. SQLGlot 30 removed both that switch and the matching
    # constructor argument; its tokenizer core accepts our Enum directly.
    if hasattr(sqlglot_tokens, "USE_RS_TOKENIZER"):
        previous_use_rs_tokenizer = getattr(sqlglot_tokens, "USE_RS_TOKENIZER")
        setattr(sqlglot_tokens, "USE_RS_TOKENIZER", False)
        try:
            class _Tokenizer(parent.Tokenizer):  # ty: ignore[unresolved-attribute]
                KEYWORDS = {**parent.Tokenizer.KEYWORDS, **MARKERS}  # ty: ignore[unresolved-attribute]

                def __init__(self, *args, **kwargs) -> None:
                    kwargs.setdefault("use_rs_tokenizer", False)
                    super().__init__(*args, **kwargs)
        finally:
            setattr(sqlglot_tokens, "USE_RS_TOKENIZER", previous_use_rs_tokenizer)
    else:
        class _Tokenizer(parent.Tokenizer):  # ty: ignore[unresolved-attribute]
            KEYWORDS = {**parent.Tokenizer.KEYWORDS, **MARKERS}  # ty: ignore[unresolved-attribute]

    # This hooks SQLGlot's private API: `Tokenizer.KEYWORDS`, `_parse_join`,
    # `_match`, `_prev`, `_curr`. It is the best seam available — SQLGlot exposes
    # no public way to extend a grammar — and it is deliberately preferred to the
    # alternatives we tried: rewriting the SQL before parsing, and matching
    # markers to joins ourselves by source offset. Both of those put the
    # correctness of the marker-to-join mapping in our own code, where SQLGlot's
    # comment placement varies with aliasing, whitespace, and the preceding
    # predicate. Here the grammar decides, so it cannot drift out of step.
    #
    # The coupling breaks in two ways, and only one of them is safe by itself.
    # If `_parse_join` is renamed or changes arity, this override stops running,
    # the marker token is left with no rule to consume it, and parsing fails —
    # loudly, and with nothing proved. But if `KEYWORDS` stops applying, the
    # marker stays an ordinary comment and simply vanishes, which would leave a
    # program reporting as proved. `_unresolved_markers` exists for that case;
    # do not remove it, and do not add a fallback that carries on without the
    # hook.
    class _Parser(parent.Parser):  # ty: ignore[unresolved-attribute]
        # The case marker is a primary expression of its own, so it attaches
        # to whatever expression follows it, which must be a simple CASE.
        PRIMARY_PARSERS = {
            **parent.Parser.PRIMARY_PARSERS,  # ty: ignore[unresolved-attribute]
            SqlassertToken.SQLASSERT_EXHAUSTIVE: lambda self, token: self._parse_exhaustive_case(token),
        }

        def _parse_exhaustive_case(self, marker: Token) -> exp.Case:
            """The simple CASE a case marker precedes.

            Its WHEN values are distinct, non-null string, numeric, and boolean
            literals: each is compared with the operand, so an arm matches a
            domain member exactly when it is that literal.
            """
            line = marker.line

            def malformed(problem: str) -> UnattachedMarker:
                return UnattachedMarker(f"the exhaustive assertion on line {line} {problem}", line, marker.text)

            if not self._match(TokenType.CASE):
                raise _misplaced_case_marker(marker)
            case = self._parse_case()
            if not isinstance(case, exp.Case):
                raise _misplaced_case_marker(marker)
            if case.this is None:
                raise malformed("marks a searched CASE; only a simple CASE operand WHEN value ... can be exhaustive")
            values: list[LiteralValue] = []
            for arm in case.args.get("ifs") or ():
                when = arm.this
                if isinstance(when, exp.Null):
                    raise malformed("has a WHEN NULL arm, which matches nothing; NOT NULL governs NULL")
                value = literal_value(when)
                if value is None:
                    raise malformed(f"has WHEN {when.sql(dialect=self.dialect)}, which is not a string, numeric, or boolean literal")
                if value in values:
                    raise malformed(f"lists WHEN {value.sql()} more than once")
                values.append(value)
            case.meta[_EXHAUSTIVE_AT] = line
            return case

        def _parse_case(self, *args, **kwargs):
            """Record where a simple CASE's marker would be written: right
            before its CASE keyword, already consumed here."""
            start = self._prev
            case = super()._parse_case(*args, **kwargs)
            if isinstance(case, exp.Case) and case.this is not None and start is not None:
                case.meta[_MARKER_POSITION] = (start.start, start.line)
            return case

        def _parse_join(self, *args, **kwargs):
            if self._curr is not None and self._curr.token_type is SqlassertToken.SQLASSERT_EXHAUSTIVE:
                raise _misplaced_case_marker(self._curr)
            if self._curr is None or self._curr.token_type not in JOIN_MARKER_KINDS:
                start = self._curr
                join = super()._parse_join(*args, **kwargs)
                # A comma join has no JOIN keyword a marker could precede.
                if join is not None and start is not None and start.token_type is not TokenType.COMMA:
                    join.meta[_MARKER_POSITION] = (start.start, start.line)
                return join

            kind = JOIN_MARKER_KINDS[self._curr.token_type]
            self._advance()
            line = self._prev.line
            unattached = UnattachedMarker(
                f"the {JOIN_MARKER_NOUNS[kind]} marker on line {line} is not followed by the JOIN keyword",
                line,
                self._prev.text,
            )

            # A comma join carries no JOIN keyword to mark, and multiplies rows
            # by definition. Marking one is a mistake, not an assertion.
            if self._curr is not None and self._curr.token_type is TokenType.COMMA:
                raise unattached

            join = super()._parse_join(*args, **kwargs)
            if join is None:
                raise unattached

            join.meta[_ASSERTED_AT] = line
            join.meta[_JOIN_MARKER_KIND] = kind
            return join

        def _parse_select_query(self, *args, **kwargs):
            """Consume every trailing column marker immediately
            after building a Select node -- the one production common to a
            Root Select, a CTE definition, a view body, and a subquery, so a
            marker means the same thing regardless of which of these it sits
            on. Only fires on an actual `exp.Select`: this method is also
            reached, recursively, while wrapping a parenthesized derived table
            into an `exp.Subquery`, which has no trailing marker of its own to
            consume.
            """
            result = super()._parse_select_query(*args, **kwargs)
            if not isinstance(result, exp.Select):
                return result

            found: list[ColumnMarker] = []
            while self._curr is not None and self._curr.token_type in _COLUMN_MARKER_OPEN_TOKENS:
                kind = _COLUMN_MARKER_OPEN_TOKENS[self._curr.token_type]
                line = self._curr.line
                self._advance()

                dependent: tuple[str, ...] = ()
                values: tuple[LiteralValue, ...] = ()
                if kind in DETERMINES_KINDS:
                    names, dependent = self._parse_marker_dependency(kind, line)
                elif kind in ENUM_KINDS:
                    names, values = self._parse_marker_domain(kind, line)
                else:
                    names = self._parse_marker_columns(kind, line)
                referenced_relation, referenced_columns = None, ()
                if kind in FOREIGN_KEY_KINDS:
                    referenced_relation, referenced_columns = self._parse_marker_reference(kind, line, len(names))
                if not self._match(SqlassertToken.SQLASSERT_MARKER_CLOSE):
                    raise UnattachedMarker(
                        f"the {MARKER_NOUNS[kind]} on line {line} is not closed with **/", line, self._prev.text
                    )

                found.append(ColumnMarker(kind, names, line, referenced_relation, referenced_columns, dependent, values))

            if found:
                result.meta[_COLUMN_MARKERS_AT] = found
            if self._curr is not None and self._curr.token_type is SqlassertToken.SQLASSERT_EXHAUSTIVE:
                raise _misplaced_case_marker(self._curr)

            # `super()._parse_select_query` already tried, and gave up, on
            # attaching a following UNION/INTERSECT/EXCEPT to this same
            # Select before returning -- its own last step runs before a
            # trailing marker exists to get in the way, so the marker token
            # sitting where it looked for a set-operation keyword reads as
            # "no set operation here". Now that the marker is consumed, retry:
            # a no-op if nothing follows, since `_parse_set_operations` only
            # advances the cursor on an actual match.
            return self._parse_set_operations(result) or result

        def _parse_set_operations(self, this, *args, **kwargs):
            """Record where a Select ends: the first time set operations are
            tried after it, before any trailing marker is consumed. This is
            also the only point that sees the left member of a set operation
            as a Select of its own.
            """
            if isinstance(this, exp.Select) and _MARKER_POSITION not in this.meta and self._prev is not None:
                this.meta[_MARKER_POSITION] = (self._prev.end + 1, self._prev.line)
            return super()._parse_set_operations(this, *args, **kwargs)

        def _parse_marker_columns(self, kind: str, line: int) -> tuple[str, ...]:
            """A marker's column list after its opening parenthesis, through the closing one."""
            columns = self._parse_csv(self._parse_id_var)
            if not columns:
                raise UnattachedMarker(f"the {MARKER_NOUNS[kind]} on line {line} names no columns", line, self._prev.text)
            names = tuple(column.name for column in columns)
            if len({name.lower() for name in names}) != len(names):
                raise UnattachedMarker(
                    f"the {MARKER_NOUNS[kind]} on line {line} names the same column more than once",
                    line,
                    self._prev.text,
                )
            if not self._match(TokenType.R_PAREN):
                raise UnattachedMarker(
                    f"the {MARKER_NOUNS[kind]} on line {line} is missing its closing parenthesis",
                    line,
                    self._prev.text,
                )
            return names

        def _parse_marker_dependency(self, kind: str, line: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
            """`determinant -> dependent`, each a column list, through the closing parenthesis."""
            determinant = tuple(column.name for column in self._parse_csv(self._parse_id_var))
            if not determinant or not self._match(TokenType.ARROW):
                raise UnattachedMarker(
                    f"the {MARKER_NOUNS[kind]} on line {line} must list columns, then ->, then the columns they determine",
                    line,
                    self._prev.text,
                )
            dependent = self._parse_marker_columns(kind, line)
            if len({name.lower() for name in determinant}) != len(determinant):
                raise UnattachedMarker(
                    f"the {MARKER_NOUNS[kind]} on line {line} names the same column more than once",
                    line,
                    self._prev.text,
                )
            return determinant, dependent

        def _parse_marker_domain(self, kind: str, line: int) -> tuple[tuple[str, ...], tuple[LiteralValue, ...]]:
            """`column IN (literal, ...)`, through the marker's closing parenthesis.

            Members are distinct, non-null string, numeric, and boolean literals.
            """

            def malformed(problem: str) -> UnattachedMarker:
                return UnattachedMarker(f"the {MARKER_NOUNS[kind]} on line {line} {problem}", line, self._prev.text)

            column = self._parse_id_var(any_token=False)
            if column is None or not self._match(TokenType.IN) or not self._match(TokenType.L_PAREN):
                raise malformed("must be written as ENUM(column IN ('value', ...))")
            expressions = tuple(self._parse_csv(self._parse_bitwise))
            if not self._match(TokenType.R_PAREN) or not self._match(TokenType.R_PAREN):
                raise malformed("must be written as ENUM(column IN ('value', ...))")
            if not expressions:
                raise malformed("lists no values")
            values: list[LiteralValue] = []
            for expression in expressions:
                if isinstance(expression, exp.Null):
                    raise malformed("lists NULL; NULL is never in an Enumerated Domain, and NOT NULL governs it")
                value = literal_value(expression)
                if value is None:
                    raise malformed(f"lists {expression.sql(dialect=self.dialect)}, which is not a string, numeric, or boolean literal")
                if value in values:
                    raise malformed(f"lists {value.sql()} more than once")
                values.append(value)
            return (column.name,), tuple(values)

        def _parse_marker_reference(self, kind: str, line: int, count: int) -> tuple[str, tuple[str, ...]]:
            """`REFERENCES relation(col, ...)`, pairing `count` columns by position."""
            missing = UnattachedMarker(
                f"the {MARKER_NOUNS[kind]} on line {line} must continue with REFERENCES relation(column, ...)",
                line,
                self._prev.text,
            )
            if not self._match(TokenType.REFERENCES):
                raise missing
            parts = [self._parse_id_var(any_token=False)]
            while parts[-1] is not None and self._match(TokenType.DOT):
                parts.append(self._parse_id_var(any_token=False))
            if None in parts or not self._match(TokenType.L_PAREN):
                raise missing
            relation = ".".join(part.name for part in parts if part is not None)
            columns = self._parse_marker_columns(kind, line)
            if len(columns) != count:
                raise UnattachedMarker(
                    f"the {MARKER_NOUNS[kind]} on line {line} pairs {count} column(s) with {len(columns)} "
                    f"referenced column(s) of {relation}",
                    line,
                    self._prev.text,
                )
            return relation, columns

    return type(
        f"SqlAssert{parent.__name__}",
        (parent,),
        {"Tokenizer": _Tokenizer, "Parser": _Parser},
    )


def _misplaced_case_marker(marker: Token) -> UnattachedMarker:
    return UnattachedMarker(
        f"the exhaustive assertion on line {marker.line} is not followed by CASE; "
        "it must sit immediately before a simple CASE expression",
        marker.line,
        marker.text,
    )


@dataclass(frozen=True)
class ParsedProgram:
    """One SQL Program. Sequence arguments may be any iterable; what is stored
    is always an immutable tuple."""

    # CREATE and ALTER TABLE statements, in program order.
    create_statements: tuple[exp.Expression, ...] = ()
    root_select: exp.Query | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "create_statements", tuple(self.create_statements))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))


class SqlParser:
    """Parses SQL text of one dialect into a SQL Program."""

    def __init__(self, dialect: str) -> None:
        self.dialect = dialect
        self.dialect_class = sqlassert_dialect(dialect)

    def parse(self, sql: str) -> ParsedProgram:
        # Reported first, and on every path: a mistyped marker is invisible to
        # the grammar, so nothing later in parsing can notice it.
        diagnostics = _unrecognized_markers(sql)

        try:
            statements = [ statement for statement in sqlglot.parse(sql, read=self.dialect_class) if statement ]
        except UnattachedMarker as error:
            return ParsedProgram(diagnostics=(*diagnostics, self._unattached(error)))
        except SqlglotError as error:
            return ParsedProgram(diagnostics=(*diagnostics, self._unparseable(sql, error)))

        creates: list[exp.Expression] = []
        selects: list[exp.Query] = []

        for statement in statements:
            if isinstance(statement, exp.Create) or _is_alter_table(statement):
                creates.append(statement)
            elif isinstance(statement, exp.Query):
                selects.append(statement)
            else:
                diagnostics.append(
                    Diagnostic(
                        diag.UNSUPPORTED_STATEMENT,
                        f"statement is not a create statement or a root select: {statement.sql(dialect=self.dialect)}",
                    )
                )

        # A marker only becomes an assertion if the dialect recognised it, so
        # reconcile what the source contains against what parsing produced.
        diagnostics.extend(_unresolved_markers(sql, statements))

        if len(selects) > 1:
            diagnostics.append(
                Diagnostic(
                    diag.MULTIPLE_ROOT_SELECTS,
                    f"a SQL program allows at most one root select, found {len(selects)}",
                )
            )
            return ParsedProgram(tuple(creates), None, tuple(diagnostics))

        return ParsedProgram(tuple(creates), selects[0] if selects else None, tuple(diagnostics))

    def _unattached(self, error: UnattachedMarker) -> Diagnostic:
        return Diagnostic(diag.UNATTACHED_MARKER, str(error), Origin(SQL, error.text, error.line))

    def _unparseable(self, sql: str, error: SqlglotError) -> Diagnostic:
        """A parse failure, naming a marker when the program contains one.

        A marker somewhere its rule never runs breaks the grammar outright,
        and the reader should not have to hunt for SQL that is not broken.
        """
        marker = self._marker_token(sql)
        if marker is None:
            return Diagnostic(diag.SQL_PARSE_FAILED, f"SQL could not be parsed: {error}")
        return Diagnostic(
            diag.SQL_PARSE_FAILED,
            f"SQL could not be parsed, and a marker is on line {marker.line}: {error}",
            Origin(SQL, marker.text, marker.line),
        )

    def _marker_token(self, sql: str) -> Token | None:
        try:
            tokens = self.dialect_class.Tokenizer().tokenize(sql)  # ty: ignore[unresolved-attribute]
        except SqlglotError:
            return None
        return next(
            (token for token in tokens if token.token_type in _FIXED_MARKER_TOKENS),
            None,
        )


def _is_recognized_marker(text: str) -> bool:
    """Whether `text` (a whole `/**...*/`-shaped span) is a marker this
    dialect's grammar actually tokenizes -- a join marker exactly,
    or a column marker's fixed open literal followed by
    its close, with a variable column list in between that this text-level
    check does not itself validate; the grammar does that once it parses.

    Whitespace runs collapse to one space because the tokenizer matches a
    multi-word literal such as `ASSERT UNIQUE` across any whitespace run.
    """
    upper = re.sub(r"\s+", " ", text.upper())
    if upper in MARKERS:
        return True
    return upper.endswith("**/") and any(upper.startswith(open_) for open_ in _COLUMN_MARKER_OPENS)


def _unrecognized_markers(sql: str) -> list[Diagnostic]:
    """Comments shaped like a marker that are not one.

    `/**…*/` is assertion syntax, so a comment written that way and left
    unrecognized is far likelier a mistyped marker than a note — and a mistyped
    marker that was silently ignored would read to its author as a proof.
    """
    # `MARKERS` also holds the fragment literals a parameterized marker
    # tokenizes as (`"/**ASSERT UNIQUE("`, `"**/"`) -- real grammar, but not
    # themselves complete, user-facing marker shapes, so this list shows only
    # whole shapes: the bare join markers, and each parameterized marker's
    # full open-to-close form.
    recognized = ", ".join(
        sorted(literal for literal, token in MARKERS.items() if token in _FIXED_MARKER_TOKENS)
        + sorted(_marker_shape(open_, kind) for open_, kind in _COLUMN_MARKER_OPENS.items())
    )
    return [
        Diagnostic(
            diag.UNRECOGNIZED_MARKER,
            f"{match.group(0)!r} on line {_line_of(sql, match.start())} is not a sqlassert marker; "
            f"{_marker_hint(match.group(0))}the markers are {recognized}",
            Origin(SQL, match.group(0), _line_of(sql, match.start())),
        )
        for match in _MARKER_SHAPED.finditer(sql)
        if not _is_recognized_marker(match.group(0))
    ]


def _marker_shape(open_: str, kind: str) -> str:
    """The whole user-facing shape of a column marker opening with `open_`."""
    reference = " REFERENCES relation(...)" if kind in FOREIGN_KEY_KINDS else ""
    columns = "... -> ..." if kind in DETERMINES_KINDS else "column IN ('value', ...)" if kind in ENUM_KINDS else "..."
    return f"{open_}{columns}){reference}**/"


def _marker_hint(text: str) -> str:
    """Point a marker written without `ASSERT`, or a `DECLARE` that only an
    Assertion can be, at its explicit spelling."""
    if re.sub(r"\s+", " ", text.upper()) == "/**DECLARE EXHAUSTIVE**/":
        return "exhaustiveness can only be asserted, as in '/**ASSERT EXHAUSTIVE**/', never declared; "
    explicit = "/**ASSERT " + text[3:]
    if _is_recognized_marker(explicit):
        return f"assertions need an explicit ASSERT, as in {explicit!r}; "
    return ""


def _unresolved_markers(sql: str, statements: list[exp.Expression]) -> list[Diagnostic]:
    """Recognized markers that did not become assertions.

    Every marker in the source should have reached a join, a Select, or a CASE, or
    raised `UnattachedMarker`. If one merely disappeared, the dialect stopped
    recognising it — and the alternative to reporting that is a program whose
    assertions were never checked reporting as proved.
    """
    written = [match for match in _MARKER_SHAPED.finditer(sql) if _is_recognized_marker(match.group(0))]
    resolved = sum(
        1
        for statement in statements
        for join in statement.find_all(exp.Join)
        if assertion_line(join) is not None
    ) + sum(
        len(select.meta.get(_COLUMN_MARKERS_AT, ()))
        for statement in statements
        for select in statement.find_all(exp.Select)
    ) + sum(
        1
        for statement in statements
        for case in statement.find_all(exp.Case)
        if exhaustive_line(case) is not None
    )
    if len(written) <= resolved:
        return []

    first = written[resolved]
    line = _line_of(sql, first.start())
    return [
        Diagnostic(
            diag.UNRECOGNIZED_MARKER,
            f"the marker on line {line} was not recognized: "
            f"{resolved} of {len(written)} markers became assertions",
            Origin(SQL, first.group(0), line),
        )
    ]


def _line_of(sql: str, offset: int) -> int:
    return sql.count("\n", 0, offset) + 1


def assertion_line(join: exp.Join) -> int | None:
    """The source line of the marker asserting a Property of this join, if any."""
    return join.meta.get(_ASSERTED_AT)


def join_marker_kind(join: exp.Join) -> str | None:
    """The kind (a value of `JOIN_MARKER_KINDS`) of the marker on this join, if any."""
    return join.meta.get(_JOIN_MARKER_KIND)


def join_origin(join: exp.Join, dialect: str) -> Origin:
    """A readable origin for an asserted join."""
    return Origin(SQL, join.sql(dialect=dialect), assertion_line(join))


def exhaustive_line(case: exp.Case) -> int | None:
    """The source line of the marker asserting this CASE exhaustive, if any."""
    return case.meta.get(_EXHAUSTIVE_AT)


def _is_alter_table(statement: exp.Expression) -> bool:
    return isinstance(statement, exp.Alter) and (statement.args.get("kind") or "").upper() == "TABLE"


def column_markers(select: exp.Select) -> tuple[ColumnMarker, ...]:
    """Every column marker written on `select` -- those whose kind is in
    `DECLARED_KINDS` are Property Declarations, the rest Assertions."""
    return tuple(select.meta.get(_COLUMN_MARKERS_AT, ()))


def marker_position(node: exp.Join | exp.Select | exp.Case) -> tuple[int, int] | None:
    """The source `(offset, line)` where a marker for this Join, Select, or
    simple CASE would be written, or None for a join no marker can precede,
    such as a comma join, and for a searched CASE."""
    return node.meta.get(_MARKER_POSITION)
