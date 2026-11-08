"""`.sqltest` files: SQL cases with inline expectations (issue 36).

A file is SQL. Everything before the first `-- case: name` is setup; each case's
Program is the setup followed by the case's lines. `-- include: path.sql`
inserts a file relative to `tests/`; `-- dialect: name` sets the dialect for the
file (in setup) or one case. `--~` annotations state the expected outcomes and
Diagnostics:

    SQL  --~ PROVED                    the markers and Diagnostics on this line
    --~ unknown-asserted-column "x"    alone: the nearest SQL line above
    --~ UNKNOWN "no domain is known"   an outcome and its explanation
    --~! sql-parse-failed              a Diagnostic with no origin line

An annotation is a comma-separated list of `PROVED "substring" ...`,
`UNKNOWN "substring" ...`, and `code "substring" ...` items. An outcome's
substrings must appear in that assertion's `evidence.explain()`; a code's, in
the Diagnostic's message. Matching is exhaustive: every assertion and every
Diagnostic (errors and warnings alike) must be annotated, and nothing else.

Collection only parses; analysis runs when the case does. Directive and
annotation text is blanked out of the Program rather than removed, so every
Program line maps back to one file line for messages and annotations.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import sqlassert
from sqlassert import AssertionReport, Report

TESTS_ROOT = Path(__file__).parent
DEFAULT_DIALECT = "duckdb"
OUTCOMES = ("PROVED", "UNKNOWN")

_DIRECTIVE = re.compile(r"^\s*--\s*(case|include|dialect):\s*(.*?)\s*$")
_CASE_NAME = re.compile(r"^[A-Za-z0-9_]+$")
_ITEM = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|([A-Za-z][A-Za-z0-9_-]*)|(,))')


class SqlTestError(Exception):
    """A malformed `.sqltest` or included file, reported at its line."""

    def __init__(self, path: Path, line: int, message: str):
        super().__init__(f"{path}:{line}: {message}")


@dataclass(frozen=True)
class Source:
    """Where a Program line came from, for messages."""

    path: Path
    line: int

    def __str__(self) -> str:
        try:
            shown = self.path.relative_to(TESTS_ROOT)
        except ValueError:
            shown = Path(self.path.name)
        return f"{shown}:{self.line}"


@dataclass(frozen=True)
class ExpectedDiagnostic:
    code: str
    substrings: tuple[str, ...] = ()

    def matches(self, code: str, message: str) -> bool:
        return code == self.code and all(part in message for part in self.substrings)

    def __str__(self) -> str:
        return " ".join([self.code, *(f'"{part}"' for part in self.substrings)])


@dataclass(frozen=True)
class ExpectedOutcome:
    outcome: str
    substrings: tuple[str, ...] = ()

    def __str__(self) -> str:
        return " ".join([self.outcome, *(f'"{part}"' for part in self.substrings)])


@dataclass
class Expectation:
    """What one Program line (or the Program as a whole) must report."""

    outcomes: list[ExpectedOutcome] = field(default_factory=list)
    diagnostics: list[ExpectedDiagnostic] = field(default_factory=list)


@dataclass
class _Line:
    text: str  # SQL with directives and annotations blanked
    source: Source
    expectation: Expectation = field(default_factory=Expectation)
    is_sql: bool = False  # can be the target of a standalone annotation


@dataclass(frozen=True)
class Case:
    name: str
    line: int  # the `-- case:` line in the .sqltest file
    dialect: str
    sql: str
    sources: tuple[Source, ...]  # Program line n is sources[n - 1]
    expectations: dict[int | None, Expectation]  # None: no origin line


# Parsing -----------------------------------------------------------------


def parse_annotation(text: str, path: Path, line: int) -> Expectation:
    """`PROVED, unknown-asserted-column "x" "y"` -> an Expectation."""
    tokens: list[tuple[str, str]] = []
    position = 0
    text = text.rstrip()
    while position < len(text):
        match = _ITEM.match(text, position)
        if not match or match.end() == position:
            raise SqlTestError(path, line, f"cannot read annotation at {text[position:]!r}")
        position = match.end()
        quoted, word, comma = match.groups()
        if quoted is not None:
            tokens.append(("string", quoted.replace('\\"', '"').replace("\\\\", "\\")))
        elif word is not None:
            tokens.append(("word", word))
        elif comma is not None:
            tokens.append(("comma", ","))
    items: list[list[tuple[str, str]]] = [[]]
    for token in tokens:
        if token[0] == "comma":
            items.append([])
        else:
            items[-1].append(token)
    expectation = Expectation()
    for item in items:
        if not item or item[0][0] != "word":
            raise SqlTestError(path, line, "each annotation item starts with an outcome or a code")
        head, rest = item[0][1], item[1:]
        if any(kind != "string" for kind, _ in rest):
            raise SqlTestError(path, line, f"{head}: only quoted substrings may follow it")
        substrings = tuple(value for _, value in rest)
        if head in OUTCOMES:
            expectation.outcomes.append(ExpectedOutcome(head, substrings))
        elif head.upper() in OUTCOMES:
            raise SqlTestError(path, line, f"outcomes are upper case: {head.upper()}")
        else:
            expectation.diagnostics.append(ExpectedDiagnostic(head, substrings))
    return expectation


def _merge(into: Expectation, more: Expectation) -> None:
    into.outcomes.extend(more.outcomes)
    into.diagnostics.extend(more.diagnostics)


def _annotation_start(text: str) -> int:
    """The index of an annotation's `--~` outside string literals, or -1."""
    quote: str | None = None
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif text.startswith("--", index):
            return index if text.startswith("--~", index) else -1
    return -1


def _read(path: Path, stack: tuple[Path, ...], root: Path) -> list[_Line | tuple[str, str, Source]]:
    """The file's lines with includes expanded; directives other than include
    come back as `(kind, value, source)` markers for `parse_file` to place."""
    lines: list[_Line | tuple[str, str, Source]] = []
    previous_sql: _Line | None = None
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        source = Source(path, number)
        directive = _DIRECTIVE.match(raw)
        if directive:
            kind, value = directive.groups()
            if kind == "include":
                target = (root / value).resolve()
                if target.suffix != ".sql":
                    raise SqlTestError(path, number, f"can only include .sql files, not {value!r}")
                if not target.is_file():
                    raise SqlTestError(path, number, f"include not found: {value} (relative to tests/)")
                if target in stack:
                    raise SqlTestError(path, number, f"include cycle through {value}")
                lines.extend(_read(target, (*stack, target), root))
                previous_sql = None
            else:
                if path.suffix == ".sql":
                    raise SqlTestError(path, number, f"-- {kind}: is only allowed in .sqltest files")
                lines.append((kind, value, source))
                previous_sql = None
            continue
        start = _annotation_start(raw)
        if start < 0:
            line = _Line(raw, source, is_sql=bool(raw.strip()) and not raw.lstrip().startswith("--"))
            if line.is_sql:
                previous_sql = line
            lines.append(line)
            continue
        body = raw[start + 3:]
        program_level = body.startswith("!")
        expectation = parse_annotation(body[1:] if program_level else body, path, number)
        sql = raw[:start]
        if program_level:
            if sql.strip():
                raise SqlTestError(path, number, "--~! stands alone on its line")
            lines.append(("program", "", source))
            lines.append(_Line("", source, expectation))
        elif sql.strip():
            line = _Line(sql.rstrip(), source, expectation, is_sql=True)
            previous_sql = line
            lines.append(line)
        else:
            if previous_sql is None:
                raise SqlTestError(path, number, "a standalone --~ needs a SQL line above it")
            _merge(previous_sql.expectation, expectation)
            lines.append(_Line("", source))
    return lines


def parse_file(path: Path, root: Path = TESTS_ROOT) -> list[Case]:
    """The cases in one `.sqltest` file; includes resolve against `root`."""
    path = path.resolve()
    entries = _read(path, (path,), root.resolve())
    setup: list[_Line] = []
    setup_program: list[Expectation] = []
    file_dialect = DEFAULT_DIALECT
    cases: list[tuple[str, Source, list[_Line], list[Expectation], str | None]] = []
    names: set[str] = set()
    program_next = False
    for entry in entries:
        if isinstance(entry, tuple):
            kind, value, source = entry
            if kind == "case":
                if not _CASE_NAME.match(value):
                    raise SqlTestError(source.path, source.line, f"case names are [A-Za-z0-9_]+: {value!r}")
                if value in names:
                    raise SqlTestError(source.path, source.line, f"duplicate case name {value!r}")
                names.add(value)
                cases.append((value, source, [], [], None))
                entry = _Line("", source)
            elif kind == "dialect":
                if not value:
                    raise SqlTestError(source.path, source.line, "-- dialect: needs a name")
                if cases:
                    name, case_source, body, program, dialect = cases[-1]
                    if dialect is not None:
                        raise SqlTestError(source.path, source.line, "a second -- dialect: in one case")
                    cases[-1] = (name, case_source, body, program, value)
                else:
                    file_dialect = value
                entry = _Line("", source)
            else:  # program-level annotation follows
                program_next = True
                continue
        if program_next:
            (cases[-1][3] if cases else setup_program).append(entry.expectation)
            entry = _Line("", entry.source)
            program_next = False
        (cases[-1][2] if cases else setup).append(entry)
    result = []
    for name, source, body, program, dialect in cases:
        lines = [*setup, *body]
        expectations: dict[int | None, Expectation] = {}
        for number, line in enumerate(lines, start=1):
            if line.expectation.outcomes or line.expectation.diagnostics:
                expectations[number] = line.expectation
        whole = Expectation()
        for expectation in (*setup_program, *program):
            _merge(whole, expectation)
        if whole.outcomes:
            raise SqlTestError(source.path, source.line, "--~! annotates Diagnostics, not outcomes")
        if whole.diagnostics:
            expectations[None] = whole
        result.append(Case(
            name, source.line, dialect or file_dialect,
            "\n".join(line.text for line in lines) + "\n",
            tuple(line.source for line in lines), expectations,
        ))
    if not result:
        raise SqlTestError(path, 1, "no -- case: in file")
    return result


# Checking ----------------------------------------------------------------


def mismatches(case: Case, report: Report) -> list[str]:
    """Every difference between the case's annotations and the Report."""
    actual_outcomes: dict[int, list[AssertionReport]] = {}
    for assertion in report.assertions:
        actual_outcomes.setdefault(assertion.origin.line or 0, []).append(assertion)
    actual_diagnostics: dict[int | None, list[tuple[str, str]]] = {}
    for diagnostic in (*report.diagnostics, *report.warnings):
        line = diagnostic.origin.line if diagnostic.origin else None
        actual_diagnostics.setdefault(line, []).append((diagnostic.code, diagnostic.message))

    def where(line: int | None) -> str:
        if line is None:
            return "program"
        if 1 <= line <= len(case.sources):
            return str(case.sources[line - 1])
        return f"Program line {line}"

    problems = []
    for line in sorted({*case.expectations, *actual_outcomes, *actual_diagnostics}, key=lambda n: -1 if n is None else n):
        expected = case.expectations.get(line, Expectation())
        assertions = actual_outcomes.get(line or 0, []) if line is not None else []
        outcomes = [assertion.outcome.name for assertion in assertions]
        wanted = [want.outcome for want in expected.outcomes]
        if outcomes != wanted:
            problems.append(f"{where(line)}: outcomes {outcomes or 'none'}, expected {wanted or 'none'}")
        else:
            for assertion, want in zip(assertions, expected.outcomes):
                text = assertion.evidence.explain() if assertion.evidence is not None else ""
                problems.extend(
                    f"{where(line)}: {want.outcome} explanation lacks {part!r}:\n{text or '(no evidence)'}"
                    for part in want.substrings if part not in text
                )
        unmatched = list(actual_diagnostics.get(line, []))
        missing = []
        # Most specific expectations first, so a bare code cannot steal the
        # Diagnostic a substring-qualified one needs.
        for want in sorted(expected.diagnostics, key=lambda d: -len(d.substrings)):
            hit = next((d for d in unmatched if want.matches(*d)), None)
            if hit is None:
                missing.append(want)
            else:
                unmatched.remove(hit)
        problems.extend(f"{where(line)}: expected {want}, not reported" for want in missing)
        problems.extend(f"{where(line)}: unexpected {code}: {message}" for code, message in unmatched)
    return problems


# Pytest ------------------------------------------------------------------


class SqlTestFile(pytest.File):
    def collect(self):
        try:
            cases = parse_file(self.path)
        except SqlTestError as error:
            raise self.CollectError(str(error)) from error
        for case in cases:
            yield SqlTestItem.from_parent(self, name=case.name, case=case)


class SqlTestItem(pytest.Item):
    def __init__(self, *, case: Case, **kwargs):
        super().__init__(**kwargs)
        self.case = case

    def runtest(self) -> None:
        # Looked up at call time, so the soundness oracle's wrapper sees it.
        report = sqlassert.analyze(self.case.sql, dialect=self.case.dialect)
        problems = mismatches(self.case, report)
        if problems:
            raise SqlTestMismatch(problems)

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, SqlTestMismatch):
            return "\n".join(excinfo.value.problems)
        return super().repr_failure(excinfo, style=style)

    def reportinfo(self):
        return self.path, self.case.line - 1, f"{self.path.name}::{self.name}"


class SqlTestMismatch(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems
