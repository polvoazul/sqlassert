"""Rewrite Edits: exact text changes to a SQL Program, placed from its tokens.

A Rewrite carries the edits that make it (`Edit`: replace the text between two
offsets of the original SQL). Edits are placed from SQLGlot's token stream,
whose tokens keep their source offsets; the AST keeps them only on
identifiers, literals and function names. An edit never deletes a comment or
a Property Marker: whatever of either lies in a span it replaces is kept in
its replacement. Everything shown of a Rewrite -- the rewritten SQL, its
diff -- is derived from its edits.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlglot.tokens import Token, TokenType

from sqlassert.sql_parse import SqlassertToken


@dataclass(frozen=True)
class Edit:
    """Replace the original SQL's text from `start` to `end` (offsets, `end`
    exclusive) with `replacement`; an insertion when they are equal."""

    start: int
    end: int
    replacement: str


def apply_edits(sql: str, edits: Iterable[Edit]) -> str:
    """`sql` with `edits` applied. They must not overlap."""
    ordered = sorted(edits, key=lambda edit: (edit.start, edit.end))
    for before, after in zip(ordered, ordered[1:]):
        if after.start < before.end:
            raise ValueError(f"overlapping edits: {before} and {after}")
    for edit in reversed(ordered):
        sql = sql[:edit.start] + edit.replacement + sql[edit.end:]
    return sql


def unified_diff(sql: str, edits: Iterable[Edit], *, name: str = "original", context: int = 2) -> str:
    """The unified diff from `sql` to `sql` with `edits` applied, with
    `@@ -10,4 +10,5 @@` hunk headers and `context` lines around each change."""
    lines = difflib.unified_diff(
        sql.splitlines(), apply_edits(sql, edits).splitlines(), f"{name}", f"{name} (rewritten)", n=context, lineterm="",
    )
    return "\n".join(lines)


# Tokens that open and close a nesting level: no clause or list separator
# inside one belongs to the level outside it.
_OPENS = {TokenType.L_PAREN, TokenType.L_BRACKET, TokenType.L_BRACE, TokenType.CASE}
_CLOSES = {TokenType.R_PAREN, TokenType.R_BRACKET, TokenType.R_BRACE, TokenType.END}


class Source:
    """The SQL text and its SQL tokens: Property Markers are left out, like
    comments, so no edit's span starts or ends inside one and every edit
    keeps them (`kept`). Token positions are indexes into `tokens`."""

    def __init__(self, sql: str, tokens: Sequence[Token]) -> None:
        self.sql = sql
        self.tokens: list[Token] = []
        marker = False
        for token in tokens:
            if isinstance(token.token_type, SqlassertToken):
                # A column marker's open token is followed by plain tokens up to its close.
                marker = token.token_type.name.endswith("_OPEN")
                continue
            if not marker:
                self.tokens.append(token)
        self._by_start = {token.start: index for index, token in enumerate(self.tokens)}

    def at(self, offset: int) -> int | None:
        """The token that starts at `offset`."""
        return self._by_start.get(offset)

    def after(self, offset: int) -> int | None:
        """The first token that starts at or after `offset`."""
        return next((index for index, token in enumerate(self.tokens) if token.start >= offset), None)

    def before(self, offset: int) -> int | None:
        """The last token that ends at or before `offset`."""
        return next((index for index in range(len(self.tokens) - 1, -1, -1) if self.tokens[index].end < offset), None)

    def is_(self, index: int | None, *types: TokenType) -> bool:
        return index is not None and 0 <= index < len(self.tokens) and self.tokens[index].token_type in types

    def start(self, index: int) -> int:
        return self.tokens[index].start

    def end(self, index: int) -> int:
        """The offset just past token `index`."""
        return self.tokens[index].end + 1

    def text(self, first: int, last: int) -> str:
        """The SQL from token `first` through token `last`, verbatim."""
        return self.sql[self.start(first):self.end(last)]

    def closing(self, index: int) -> int | None:
        """The token that closes the nesting level token `index` opens."""
        depth = 0
        for current in range(index, len(self.tokens)):
            kind = self.tokens[current].token_type
            depth += kind in _OPENS
            depth -= kind in _CLOSES
            if depth == 0:
                return current
        return None

    def opening(self, index: int) -> int | None:
        """The token that opens the nesting level token `index` closes."""
        depth = 0
        for current in range(index, -1, -1):
            kind = self.tokens[current].token_type
            depth += kind in _CLOSES
            depth -= kind in _OPENS
            if depth == 0:
                return current
        return None

    def top_level(self, first: int, last: int, *types: TokenType) -> list[int]:
        """The tokens of `types` from `first` through `last` outside every
        nesting level that opens there. An AND that closes a BETWEEN is not one."""
        found = []
        depth = 0
        between = 0
        for index in range(first, last + 1):
            kind = self.tokens[index].token_type
            if kind in _CLOSES:
                depth -= 1
            elif kind in _OPENS:
                depth += 1
            elif depth == 0 and kind is TokenType.BETWEEN:
                between += 1
            elif depth == 0 and kind is TokenType.AND and between:
                between -= 1
            elif depth == 0 and kind in types:
                found.append(index)
        return found

    def split(self, first: int, last: int, separator: TokenType) -> list[tuple[int, int]]:
        """The `(first, last)` tokens of each part of `first`..`last` between
        top-level `separator` tokens."""
        parts, start = [], first
        for index in self.top_level(first, last, separator):
            parts.append((start, index - 1))
            start = index + 1
        parts.append((start, last))
        return parts

    def kept(self, start: int, end: int, moved: Iterable[tuple[int, int]] = ()) -> list[str]:
        """The comments and Property Markers between offsets `start` and `end`,
        outside the SQL tokens: what an edit there must keep. One that ends a
        line ends with its line break, so a `--` comment still ends there.
        Those inside a `moved` token range are not: the edit writes that text
        elsewhere, verbatim."""
        spans = [(self.start(first), self.end(last)) for first, last in moved]
        pieces = []
        cursor = start
        for token in self.tokens:
            if token.end < start or token.start >= end:
                continue
            if not any(left <= cursor and token.start <= right for left, right in spans):
                pieces.extend(self._kept_in(cursor, token.start))
            cursor = token.end + 1
        pieces.extend(self._kept_in(cursor, end))
        return pieces

    def _kept_in(self, start: int, end: int) -> list[str]:
        gap = self.sql[start:end]
        text = gap.strip()
        if not text:
            return []
        trailing = gap[len(gap.rstrip()):]
        return [text + ("\n" if "\n" in trailing else "")]

    def replace(self, first: int, last: int, text: str, moved: Iterable[tuple[int, int]] = ()) -> Edit:
        """Tokens `first` through `last` replaced by `text`, keeping the
        comments and markers between them, but those `moved` (`kept`), after it."""
        start, end = self.start(first), self.end(last)
        kept = self.kept(start, end, moved)
        return Edit(start, end, _joined([text, *kept]) if text else _joined(kept))

    def delete(self, first: int, last: int, moved: Iterable[tuple[int, int]] = ()) -> Edit:
        """Tokens `first` through `last` deleted, with the whitespace on one
        side of them (the line's, when they start one), keeping any comment or
        marker between them but those `moved` (`kept`)."""
        start, end = self.start(first), self.end(last)
        leading = len(self.sql[:start]) - len(self.sql[:start].rstrip())
        trailing = len(self.sql[end:]) - len(self.sql[end:].lstrip())
        kept = self.kept(start, end, moved)
        # Whitespace on one side only goes when nothing then runs into the
        # deleted text's neighbour: `(... ORDER BY a)`, not `(...), b AS`.
        before = self.sql[start - leading - 1:start - leading]
        after = self.sql[end + trailing:end + trailing + 1]
        if leading and not trailing and after not in {"", ")", ",", ";"}:
            leading = 0
        if trailing and not leading and before not in {"", "("}:
            trailing = 0
        if trailing and after and (not leading or "\n" in self.sql[start - leading:start]):
            return Edit(start, end + trailing, _joined(kept) + (" " if kept and not kept[-1].endswith("\n") else ""))
        if leading:
            return Edit(start - leading, end, (" " if kept else "") + _joined(kept))
        return Edit(start, end, _joined(kept))

    def tokens_of(self, span: tuple[int, int] | None) -> tuple[int, int] | None:
        """The first and last tokens of a `(start, end)` source span."""
        if span is None:
            return None
        first, last = self.at(span[0]), self.before(span[1])
        return None if first is None or last is None or last < first else (first, last)

    def deletions(self, ranges: Iterable[tuple[int, int]]) -> tuple[Edit, ...]:
        """Each `(first, last)` token range deleted (`delete`), ranges with no
        token between them as one."""
        merged: list[tuple[int, int]] = []
        for first, last in sorted(ranges):
            if merged and first <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
            else:
                merged.append((first, last))
        return tuple(self.delete(first, last) for first, last in merged)

    def insert_before(self, index: int, text: str) -> Edit:
        return Edit(self.start(index), self.start(index), text)

    def insert_after(self, index: int, text: str) -> Edit:
        return Edit(self.end(index), self.end(index), text)


def _joined(pieces: list[str]) -> str:
    text = ""
    for piece in pieces:
        text += piece if not text or text.endswith("\n") else " " + piece
    return text

