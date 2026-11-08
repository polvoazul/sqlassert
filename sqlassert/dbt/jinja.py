"""Render the small Jinja subset most dbt models are written in, without dbt.

A model's compiled code is one rendering of its raw code: an incremental
model's `is_incremental()` branch is taken or not, and the manifest holds only
the one dbt rendered. To analyze the other, and to map a compiled line back to
the line of the model file, this module renders raw code itself.

Supported: data, `{# comments #}`, `{{ ref(...) }}`, `{{ source(...) }}`,
`{{ this }}`, `{{ config(...) }}` (which renders nothing), and
`{% if [not] is_incremental() %} ... {% else %} ... {% endif %}`, with
whitespace control. Anything else raises `Unrenderable`: a variable, a
filter, a loop or another macro could render anything, and guessing what
would be unsound. A caller trusts a rendering only after the same renderer
reproduced dbt's own compiled code for the branch dbt took.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass

# Renders `ref`, `source` or `this` (with no arguments) from its literal arguments.
type Resolver = Callable[[str, tuple[object, ...], dict[str, object]], str]


class Unrenderable(Exception):
    """Raw code outside the supported subset."""


@dataclass(frozen=True)
class Rendered:
    """Rendered text, and for each of its characters the raw-code offset it was copied from."""

    text: str
    sources: tuple[int, ...]

    def raw_offset(self, offset: int) -> int | None:
        """The raw-code offset of rendered `offset`, or None past the end or
        for text a tag produced (a resolved ref, say), which no raw line wrote."""
        return self.sources[offset] if 0 <= offset < len(self.sources) and self.sources[offset] >= 0 else None


_CLOSERS = {"{{": "}}", "{%": "%}", "{#": "#}"}


def has_incremental_branch(raw: str) -> bool:
    return "is_incremental" in raw


def render(raw: str, *, incremental: bool, resolve: Resolver) -> Rendered:
    text: list[str] = []
    sources: list[int] = []
    # Each open `if`: whether its current branch renders.
    branches: list[bool] = []
    strip_next = False
    position = 0
    while True:
        start = _next_tag(raw, position)
        data_end = len(raw) if start < 0 else start
        opener = "" if start < 0 else raw[start:start + 2]
        left_strip = start >= 0 and raw.startswith("-", start + 2)
        data = range(position, data_end)
        if strip_next:
            data = range(_skip_space(raw, position, data_end), data_end)
        if left_strip:
            data = range(data.start, _trim_space(raw, data.start, data.stop))
        if all(branches):
            text.extend(raw[index] for index in data)
            sources.extend(data)
        if start < 0:
            break
        end = _tag_end(raw, start, _CLOSERS[opener])
        body = raw[start + 2:end].removeprefix("-")
        strip_next = body.endswith("-")
        body = body.removesuffix("-").strip()
        position = end + 2
        match opener:
            case "{#":
                pass
            case "{{":
                if all(branches):
                    rendered = _expression(body, resolve)
                    text.extend(rendered)
                    sources.extend([-1] * len(rendered))
            case _:
                _statement(body, branches, incremental)
    if branches:
        raise Unrenderable("an {% if %} is never closed")
    # Jinja drops a single trailing newline (`keep_trailing_newline` is off in dbt).
    if text and text[-1] == "\n":
        text.pop()
        sources.pop()
    return Rendered("".join(text), tuple(sources))


def _next_tag(raw: str, position: int) -> int:
    found = [index for opener in _CLOSERS if (index := raw.find(opener, position)) >= 0]
    return min(found, default=-1)


def _tag_end(raw: str, start: int, closer: str) -> int:
    """The offset of `closer` ending the tag opened at `start`, skipping quoted text."""
    index, quote = start + 2, None
    while index < len(raw):
        char = raw[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"" and closer != "#}":
            quote = char
        elif raw.startswith(closer, index):
            return index
        index += 1
    raise Unrenderable(f"a tag opened at offset {start} is never closed")


def _skip_space(raw: str, start: int, stop: int) -> int:
    while start < stop and raw[start].isspace():
        start += 1
    return start


def _trim_space(raw: str, start: int, stop: int) -> int:
    while stop > start and raw[stop - 1].isspace():
        stop -= 1
    return stop


def _expression(body: str, resolve: Resolver) -> str:
    try:
        tree = ast.parse(body, mode="eval").body
    except SyntaxError as error:
        raise Unrenderable(f"{{{{ {body} }}}} is not a supported expression") from error
    match tree:
        case ast.Name(id="this"):
            return resolve("this", (), {})
        case ast.Call(func=ast.Name(id="config")):
            return ""
        case ast.Call(func=ast.Name(id="ref" | "source" as name), args=args, keywords=keywords):
            values = tuple(_literal(argument, body) for argument in args)
            options = {keyword.arg: _literal(keyword.value, body) for keyword in keywords if keyword.arg is not None}
            if len(options) != len(keywords):
                raise Unrenderable(f"{{{{ {body} }}}} passes **arguments")
            return resolve(name, values, options)
        case _:
            raise Unrenderable(f"{{{{ {body} }}}} is not a ref, source, this or config")


def _literal(node: ast.expr, body: str) -> object:
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
        return node.value
    raise Unrenderable(f"{{{{ {body} }}}} has an argument that is not a literal")


def _statement(body: str, branches: list[bool], incremental: bool) -> None:
    keyword, _, rest = body.partition(" ")
    match keyword:
        case "if":
            branches.append(_condition(rest.strip(), incremental))
        case "else" if branches and not rest.strip():
            branches[-1] = not branches[-1]
        case "endif" if branches and not rest.strip():
            branches.pop()
        case _:
            raise Unrenderable(f"{{% {body} %}} is not an is_incremental() branch")


def _condition(text: str, incremental: bool) -> bool:
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError as error:
        raise Unrenderable(f"{{% if {text} %}} is not a supported condition") from error
    match tree:
        case ast.Call(func=ast.Name(id="is_incremental"), args=[], keywords=[]):
            return incremental
        case ast.UnaryOp(op=ast.Not(), operand=ast.Call(func=ast.Name(id="is_incremental"), args=[], keywords=[])):
            return not incremental
        case _:
            raise Unrenderable(f"{{% if {text} %}} is not is_incremental()")
