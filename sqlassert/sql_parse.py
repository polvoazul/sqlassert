"""Parse SQL text into a SQL Program: Create Statements plus at most one Root
Select, with Unique Join Assertion markers resolved onto the joins they mark.

The marker is real syntax, not a comment we go looking for: a custom dialect
tokenizes `/**UNIQUE**/` as a token of its own and the join rule consumes it, so
the grammar is what requires a marker to sit immediately before its join. The
line is then recorded on the `Join` node, and nothing downstream re-reads SQL
text. SQLGlot objects stop at this boundary; only the IR parser reads them.
"""

from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
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
from sqlassert.provenance import SQL, Origin

# Where a resolved assertion is recorded on its Join node.
_ASSERTED_AT = "sqlassert.asserted_at"

# A comment opening with `/**` is how an author writes an assertion.
_MARKER_SHAPED = re.compile(r"/\*\*.*?\*/", re.DOTALL)


class SqlassertToken(enum.Enum):
    """Token types sqlassert adds to a dialect.

    Deliberately a plain Enum: SQLGlot's own `TokenType` is an `IntEnum`, so a
    member of ours sharing its integer value would compare equal to a real token
    type and be swallowed by SQLGlot's keyword sets. A plain member can equal
    nothing but itself, which also means no rule but ours can consume it.
    """

    SQLASSERT_UNIQUE = enum.auto()
    SQLASSERT_UNIQUE_SET_OPEN = enum.auto()
    SQLASSERT_CANDIDATE_KEY_OPEN = enum.auto()
    SQLASSERT_MARKER_CLOSE = enum.auto()


# Every marker sqlassert understands, and the token each becomes. The single
# source of truth for the *bare* Unique Join marker and the fixed opening/
# closing literals of a Unique Set Assertion marker: the dialect tokenizes
# exactly these, and `_unrecognized_markers` reports anything else shaped
# like one.
#
# A Unique Set Assertion marker -- `/**UNIQUE(col, ...)**/` or
# `/**PRIMARY KEY(col, ...)**/` -- carries a variable column list, so unlike
# the bare join marker it cannot be one fixed `KEYWORDS` literal: only its
# open and close are fixed text; the column list between them is ordinary SQL,
# parsed with the parser's own identifier-list machinery once the open token
# is matched (`_Parser._parse_select_query` below).
MARKERS = {
    "/**UNIQUE**/": SqlassertToken.SQLASSERT_UNIQUE,
    "/**UNIQUE(": SqlassertToken.SQLASSERT_UNIQUE_SET_OPEN,
    "/**PRIMARY KEY(": SqlassertToken.SQLASSERT_CANDIDATE_KEY_OPEN,
    "**/": SqlassertToken.SQLASSERT_MARKER_CLOSE,
}

# The open literals of a Unique Set Assertion marker, and the assertion kind
# each one states -- "unique" for a nullable-tolerant Unique Set, "key" for
# the stricter, non-null Candidate Key.
_UNIQUE_SET_MARKER_OPENS = {
    "/**UNIQUE(": "unique",
    "/**PRIMARY KEY(": "key",
}

# Where a resolved Unique Set Assertion is recorded on its Select node.
_UNIQUE_SET_ASSERTED_AT = "sqlassert.unique_set_asserted_at"

_UNIQUE_SET_OPEN_TOKENS = (SqlassertToken.SQLASSERT_UNIQUE_SET_OPEN, SqlassertToken.SQLASSERT_CANDIDATE_KEY_OPEN)


class UnattachedMarker(ParseError):
    """A marker that does not mark a join."""

    def __init__(self, message: str, line: int, text: str) -> None:
        super().__init__(message)
        self.line = line
        self.text = text


@lru_cache(maxsize=None)
def sqlassert_dialect(base: str) -> type[Dialect]:
    """The named dialect, plus Unique Join Assertion markers as real syntax.

    Cached because SQLGlot registers every dialect class by name, so one class
    per base dialect must be built once rather than per analysis.
    """
    parent = type(Dialect.get_or_raise(base))

    # `Dialect` does not declare `Tokenizer`/`Parser` as class attributes --
    # every concrete dialect sets its own, so the base class's static type
    # cannot know they exist. This is exactly the private-API coupling
    # documented below, not a type worth narrowing further.
    #
    # `SQLASSERT_UNIQUE` is deliberately not a `TokenType` (see above), but
    # SQLGlot's Rust tokenizer -- used whenever the optional `sqlglotrs`
    # package is installed -- indexes every `KEYWORDS` value against
    # `TokenType` while building the class, and KeyErrors on ours. Building
    # this one class with the Rust tokenizer disabled sidesteps that; pinning
    # its instances to the Python tokenizer keeps them consistent, since no
    # Rust settings were ever built for this `KEYWORDS` dict.
    previous_use_rs_tokenizer = sqlglot_tokens.USE_RS_TOKENIZER
    sqlglot_tokens.USE_RS_TOKENIZER = False
    try:
        class _Tokenizer(parent.Tokenizer):  # ty: ignore[unresolved-attribute]
            KEYWORDS = {**parent.Tokenizer.KEYWORDS, **MARKERS}  # ty: ignore[unresolved-attribute]

            def __init__(self, *args, **kwargs) -> None:
                kwargs.setdefault("use_rs_tokenizer", False)
                super().__init__(*args, **kwargs)
    finally:
        sqlglot_tokens.USE_RS_TOKENIZER = previous_use_rs_tokenizer

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
        def _parse_join(self, *args, **kwargs):
            if not self._match(SqlassertToken.SQLASSERT_UNIQUE):
                return super()._parse_join(*args, **kwargs)

            line = self._prev.line
            unattached = UnattachedMarker(
                f"the unique join marker on line {line} is not followed by the JOIN keyword",
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
            return join

        def _parse_select_query(self, *args, **kwargs):
            """Consume every trailing Unique Set Assertion marker immediately
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

            found: list[tuple[str, tuple[str, ...], int]] = []
            while self._curr is not None and self._curr.token_type in _UNIQUE_SET_OPEN_TOKENS:
                kind = "key" if self._curr.token_type is SqlassertToken.SQLASSERT_CANDIDATE_KEY_OPEN else "unique"
                line = self._curr.line
                self._advance()

                columns = self._parse_csv(self._parse_id_var)
                if not columns:
                    raise UnattachedMarker(
                        f"the unique set marker on line {line} names no columns", line, self._prev.text
                    )
                names = tuple(column.name for column in columns)
                if len({name.lower() for name in names}) != len(names):
                    raise UnattachedMarker(
                        f"the unique set marker on line {line} names the same column more than once",
                        line,
                        self._prev.text,
                    )
                self._match_r_paren()
                if not self._match(SqlassertToken.SQLASSERT_MARKER_CLOSE):
                    raise UnattachedMarker(
                        f"the unique set marker on line {line} is not closed with **/", line, self._prev.text
                    )

                found.append((kind, names, line))

            if found:
                result.meta[_UNIQUE_SET_ASSERTED_AT] = found
            return result

    return type(
        f"SqlAssert{parent.__name__}",
        (parent,),
        {"Tokenizer": _Tokenizer, "Parser": _Parser},
    )


@dataclass(frozen=True)
class ParsedProgram:
    """One SQL Program. Sequence arguments may be any iterable; what is stored
    is always an immutable tuple."""

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
            if isinstance(statement, exp.Create):
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

        A marker somewhere the join rule never runs breaks the grammar outright,
        and the reader should not have to hunt for SQL that is not broken.
        """
        marker = self._marker_token(sql)
        if marker is None:
            return Diagnostic(diag.SQL_PARSE_FAILED, f"SQL could not be parsed: {error}")
        return Diagnostic(
            diag.SQL_PARSE_FAILED,
            f"SQL could not be parsed, and a unique join marker is on line {marker.line}: {error}",
            Origin(SQL, marker.text, marker.line),
        )

    def _marker_token(self, sql: str) -> Token | None:
        try:
            tokens = self.dialect_class.Tokenizer().tokenize(sql)  # ty: ignore[unresolved-attribute]
        except SqlglotError:
            return None
        return next(
            (token for token in tokens if token.token_type is SqlassertToken.SQLASSERT_UNIQUE),
            None,
        )


def _is_recognized_marker(text: str) -> bool:
    """Whether `text` (a whole `/**...*/`-shaped span) is a marker this
    dialect's grammar actually tokenizes -- the bare Unique Join marker
    exactly, or a Unique Set Assertion marker's fixed open literal followed by
    its close, with a variable column list in between that this text-level
    check does not itself validate; the grammar does that once it parses.
    """
    upper = text.upper()
    if upper in MARKERS:
        return True
    return upper.endswith("**/") and any(upper.startswith(open_) for open_ in _UNIQUE_SET_MARKER_OPENS)


def _unrecognized_markers(sql: str) -> list[Diagnostic]:
    """Comments shaped like a marker that are not one.

    `/**…*/` is assertion syntax, so a comment written that way and left
    unrecognized is far likelier a mistyped marker than a note — and a mistyped
    marker that was silently ignored would read to its author as a proof.
    """
    # `MARKERS` also holds the fragment literals a parameterized marker
    # tokenizes as (`"/**UNIQUE("`, `"**/"`) -- real grammar, but not
    # themselves complete, user-facing marker shapes, so this list shows only
    # whole shapes: the bare join marker, and each parameterized marker's
    # full open-to-close form.
    recognized = ", ".join(
        sorted(literal for literal, token in MARKERS.items() if token is SqlassertToken.SQLASSERT_UNIQUE)
        + sorted(f"{open_}...)**/" for open_ in _UNIQUE_SET_MARKER_OPENS)
    )
    return [
        Diagnostic(
            diag.UNRECOGNIZED_MARKER,
            f"{match.group(0)!r} on line {_line_of(sql, match.start())} is not a sqlassert marker; "
            f"the markers are {recognized}",
            Origin(SQL, match.group(0), _line_of(sql, match.start())),
        )
        for match in _MARKER_SHAPED.finditer(sql)
        if not _is_recognized_marker(match.group(0))
    ]


def _unresolved_markers(sql: str, statements: list[exp.Expression]) -> list[Diagnostic]:
    """Recognized markers that did not become assertions.

    Every marker in the source should have reached a join or a Select, or
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
        len(select.meta.get(_UNIQUE_SET_ASSERTED_AT, ()))
        for statement in statements
        for select in statement.find_all(exp.Select)
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
    """The source line asserting this join is unique, if it is asserted at all."""
    return join.meta.get(_ASSERTED_AT)


def join_origin(join: exp.Join, dialect: str) -> Origin:
    """A readable origin for an asserted join."""
    return Origin(SQL, join.sql(dialect=dialect), assertion_line(join))


def unique_set_assertions(select: exp.Select) -> tuple[tuple[str, tuple[str, ...], int], ...]:
    """Every Unique Set Assertion marker written on `select`, as
    `(kind, columns, line)` -- `kind` is `"unique"` or `"key"`."""
    return tuple(select.meta.get(_UNIQUE_SET_ASSERTED_AT, ()))
