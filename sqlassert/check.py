"""`sqlassert check`: analyze SQL Programs for CI, and render what they find.

Each file is one SQL Program, analyzed from its SQL alone (`analyze`). Every
`UNKNOWN` assertion and every Diagnostic becomes a Finding; a Connection
Warning is a warning-level Finding and never fails the check, as it never fails
a `Report`. The exit codes, Findings and formatters are this module's
interface for any command that checks SQL (the CLI, the `sqlassert-dbt` plugin);
`__main__.py` only parses arguments.

Configuration comes from `[tool.sqlassert]` in the nearest `pyproject.toml`
that has one, searched from the working directory upwards; command-line flags
replace its values.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote

from sqlglot.dialects.dialect import Dialect

from sqlassert import diagnostics as diag
from sqlassert.diagnostics import UNSUPPORTED_CONSTRUCT, Diagnostic
from sqlassert.main import DEFAULT_DIALECT, analyze
from sqlassert.provenance import Origin, Span
from sqlassert.reporting import ASSERTED_KINDS, Outcome, Report

# Exit codes, shared by every command that checks SQL.
EXIT_OK = 0  # every assertion PROVED and no Diagnostic (warnings allowed)
EXIT_FINDINGS = 1  # an assertion UNKNOWN or a Diagnostic reported
EXIT_USAGE = 2  # bad arguments or configuration, or SQL that could not be read


def unproved(kind: str) -> str:
    """The code of the Finding an `UNKNOWN` assertion of this marker kind
    produces: it names what was asserted and not proved, e.g. `unproved-unique`."""
    return f"unproved-{kind}"


# Every code a Finding can carry, so a mistyped ignored code is reported rather than ignored.
UNPROVED_CODES = frozenset(unproved(kind) for kind in ASSERTED_KINDS.values())
CODES = frozenset(
    UNPROVED_CODES | {value for name, value in vars(diag).items() if name.isupper() and isinstance(value, str)}
)

STDIN = "-"


class Severity(Enum):
    """An `error` fails the check; a `warning` (a Connection Warning) never does."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One thing a check reports: an `UNKNOWN` assertion or a Diagnostic.

    `path` is the file as shown to the user. `evidence` is an `UNKNOWN`
    assertion's explained Evidence, printed only when asked for.
    """

    path: str
    severity: Severity
    code: str
    message: str
    origin: Origin | None = None
    evidence: str | None = None


@dataclass(frozen=True)
class Checked:
    """One SQL Program's Report and the Findings it produced, ignored codes removed."""

    path: str
    report: Report
    findings: tuple[Finding, ...]


class UsageError(Exception):
    """Arguments, configuration, or files that leave nothing to check: exit `EXIT_USAGE`."""


# Checking ------------------------------------------------------------------


def findings(path: str, sql: str, report: Report, *, ignore: Iterable[str] = ()) -> tuple[Finding, ...]:
    """The Findings of one Report, in source order within each kind, codes in `ignore` left out."""
    ignored = frozenset(ignore)
    unknown = [
        Finding(
            path, Severity.ERROR, unproved(assertion.kind),
            f"{_written(sql, assertion.origin)} is not proved (UNKNOWN)"
            + (f": {assertion.unsupported} is not supported by sqlassert yet" if assertion.unsupported else ""),
            assertion.origin, assertion.evidence.explain() if assertion.evidence is not None else None,
        )
        for assertion in report.assertions
        if assertion.outcome is Outcome.UNKNOWN
    ]
    reported = [_diagnostic(path, diagnostic, Severity.ERROR) for diagnostic in report.diagnostics]
    # An unsupported construct is already named on the assertion it left UNKNOWN.
    warned = [_diagnostic(path, warning, Severity.WARNING) for warning in report.warnings if warning.code != UNSUPPORTED_CONSTRUCT]
    return tuple(
        finding for finding in sorted((*unknown, *reported, *warned), key=_source_order)
        if finding.code not in ignored
    )


def check(path: str, sql: str, *, dialect: str = DEFAULT_DIALECT, ignore: Iterable[str] = ()) -> Checked:
    """Analyze one SQL Program from its SQL alone."""
    report = analyze(sql, dialect=dialect)
    return Checked(path, report, findings(path, sql, report, ignore=ignore))


def exit_code(checked: Iterable[Checked]) -> int:
    """`EXIT_FINDINGS` when any error-level Finding remains, else `EXIT_OK`."""
    failed = any(finding.severity is Severity.ERROR for result in checked for finding in result.findings)
    return EXIT_FINDINGS if failed else EXIT_OK


def _diagnostic(path: str, diagnostic: Diagnostic, severity: Severity) -> Finding:
    return Finding(path, severity, diagnostic.code, diagnostic.message, diagnostic.origin)


def _source_order(finding: Finding) -> tuple[int, int]:
    """Findings without a line first, then by line and column."""
    origin = finding.origin
    if origin is None or origin.line is None:
        return 0, 0
    return origin.line, origin.span.column if origin.span is not None else 0


def _written(sql: str, origin: Origin) -> str:
    """The marker as written at `origin`, whitespace collapsed; its detail when it has no span."""
    if origin.span is None:
        return origin.detail
    return " ".join(_text(sql, origin.span).split())


def _text(sql: str, span: Span) -> str:
    return sql[_offset(sql, span.line, span.column):_offset(sql, span.end_line, span.end_column)]


def _offset(sql: str, line: int, column: int) -> int:
    """The inverse of `Span.of`: lines end at `\\n` only."""
    start = 0
    for _ in range(line - 1):
        start = sql.index("\n", start) + 1
    return start + column - 1


# Configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """`[tool.sqlassert]`: `dialect`, `paths` (files, directories, globs;
    relative to `root`), and `ignore` (codes whose Findings are dropped)."""

    root: Path
    dialect: str = DEFAULT_DIALECT
    paths: tuple[str, ...] = ()
    ignore: frozenset[str] = field(default_factory=frozenset)


_CONFIG_KEYS = {"dialect": str, "paths": list, "ignore": list}


def load_config(start: Path) -> Config:
    """The `[tool.sqlassert]` table of the nearest `pyproject.toml` that has
    one, from `start` upwards; defaults rooted at `start` when none does."""
    for directory in (start, *start.parents):
        pyproject = directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            table = tomllib.loads(pyproject.read_text()).get("tool", {}).get("sqlassert")
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise UsageError(f"{pyproject}: {error}") from error
        if table is not None:
            return _config(pyproject, table)
    return Config(start)


def _config(pyproject: Path, table: object) -> Config:
    if not isinstance(table, dict):
        raise UsageError(f"{pyproject}: [tool.sqlassert] must be a table")
    for key, value in table.items():
        expected = _CONFIG_KEYS.get(key)
        if expected is None:
            raise UsageError(f"{pyproject}: unknown key {key!r} in [tool.sqlassert]; the keys are {', '.join(_CONFIG_KEYS)}")
        if not isinstance(value, expected) or (expected is list and not all(isinstance(item, str) for item in value)):
            shape = "a string" if expected is str else "a list of strings"
            raise UsageError(f"{pyproject}: [tool.sqlassert] {key} must be {shape}")
    return Config(
        pyproject.parent,
        dialect=table.get("dialect", DEFAULT_DIALECT),
        paths=tuple(table.get("paths", ())),
        ignore=frozenset(table.get("ignore", ())),
    )


def validate(dialect: str, ignore: Iterable[str]) -> None:
    """Reject a dialect SQLGlot does not know, and an ignored code no Finding carries."""
    try:
        Dialect.get_or_raise(dialect)
    except ValueError as error:
        raise UsageError(str(error)) from error
    unknown = sorted(set(ignore) - CODES)
    if unknown:
        raise UsageError(f"unknown code(s) to ignore: {', '.join(unknown)}; the codes are {', '.join(sorted(CODES))}")


def expand(patterns: Sequence[str], root: Path) -> list[Path]:
    """The SQL files `patterns` name, relative to `root`, in order and without repeats.

    A file is itself; a directory is every `*.sql` under it; anything with a
    glob character is a recursive glob (`**` crosses directories). A pattern
    that names no file is a usage error: a check that silently checks nothing
    would pass.
    """
    found: dict[Path, None] = {}
    for pattern in patterns:
        if pattern == STDIN:
            found[Path(STDIN)] = None
            continue
        path = root / pattern
        if glob.has_magic(pattern):
            matches = sorted(Path(match) for match in glob.glob(str(path), recursive=True) if Path(match).is_file())
        elif path.is_dir():
            matches = sorted(path.rglob("*.sql"))
        elif path.is_file():
            matches = [path]
        else:
            raise UsageError(f"{pattern}: no such file or directory")
        if not matches:
            raise UsageError(f"{pattern} matches no SQL files")
        found.update(dict.fromkeys(matches))
    return list(found)


def display(path: Path) -> str:
    """How a checked file is named in output: relative to the working directory when inside it."""
    if path == Path(STDIN):
        return "<stdin>"
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def read_sql(path: Path) -> str:
    """A SQL Program's text; `-` reads stdin."""
    if path == Path(STDIN):
        return sys.stdin.read()
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError) as error:
        raise UsageError(f"{path}: {error}") from error


# Output --------------------------------------------------------------------


def indented(text: str, prefix: str = "    ") -> str:
    """Explained Evidence, set under the line it explains."""
    return "\n".join(prefix + line for line in text.splitlines())


def render_text(checked: Sequence[Checked], *, explain: bool = False) -> str:
    """`path:line:column: severity code: message`, then a summary line."""
    lines = []
    for result in checked:
        for finding in result.findings:
            lines.append(f"{_place(finding)}: {finding.severity.value} {finding.code}: {finding.message}")
            if explain and finding.evidence is not None:
                lines.append(indented(finding.evidence))
    lines.append(_summary_line(checked))
    return "\n".join(lines) + "\n"


def render_json(checked: Sequence[Checked], *, explain: bool = False) -> str:
    """Every assertion's outcome and every Finding, as one JSON document."""
    def finding(item: Finding) -> dict:
        shown = {
            "path": item.path, "severity": item.severity.value, "code": item.code, "message": item.message,
            "location": _location(item.origin),
        }
        if explain and item.evidence is not None:
            shown["evidence"] = item.evidence
        return shown

    document = {
        "assertions": [
            {"path": result.path, "id": assertion.assertion_id, "outcome": assertion.outcome.value, "location": _location(assertion.origin)}
            for result in checked for assertion in result.report.assertions
        ],
        "findings": [finding(item) for result in checked for item in result.findings],
        "summary": _summary(checked),
    }
    return json.dumps(document, indent=2) + "\n"


def render_sarif(checked: Sequence[Checked], *, explain: bool = False) -> str:
    """A SARIF 2.1.0 log, as GitHub code scanning reads it. Columns count characters."""
    found = [finding for result in checked for finding in result.findings]
    codes = sorted({finding.code for finding in found})
    driver: dict = {"name": "sqlassert", "informationUri": "https://github.com/polvoazul/sqlassert", "rules": [
        {"id": code, "shortDescription": {"text": _RULE_DESCRIPTIONS.get(code, code.replace("-", " "))}}
        for code in codes
    ]}
    try:
        driver["version"] = version("sqlassert")
    except PackageNotFoundError:
        pass

    def result(finding: Finding) -> dict:
        message = finding.message
        if explain and finding.evidence is not None:
            message += "\n\n" + finding.evidence
        location: dict = {"artifactLocation": {"uri": _uri(finding.path)}}
        origin = finding.origin
        if origin is not None and origin.line is not None:
            region: dict = {"startLine": origin.line}
            if origin.span is not None:
                region.update(
                    startColumn=origin.span.column, endLine=origin.span.end_line, endColumn=origin.span.end_column
                )
            location["region"] = region
        return {
            "ruleId": finding.code,
            "ruleIndex": codes.index(finding.code),
            "level": finding.severity.value,
            "message": {"text": message},
            "locations": [{"physicalLocation": location}],
        }

    log = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": driver}, "columnKind": "unicodeCodePoints", "results": [result(f) for f in found]}],
    }
    return json.dumps(log, indent=2) + "\n"


def render_github(checked: Sequence[Checked], *, explain: bool = False) -> str:
    """GitHub Actions workflow commands: one `::error` or `::warning` annotation per Finding."""
    lines = []
    for result in checked:
        for finding in result.findings:
            properties = {"file": finding.path}
            origin = finding.origin
            if origin is not None and origin.line is not None:
                properties["line"] = str(origin.line)
                span = origin.span
                if span is not None:
                    properties["endLine"] = str(span.end_line)
                    # GitHub honours columns only within one line.
                    if span.end_line == span.line:
                        properties.update(col=str(span.column), endColumn=str(span.end_column))
            properties["title"] = f"sqlassert {finding.code}"
            message = finding.message
            if explain and finding.evidence is not None:
                message += "\n" + finding.evidence
            shown = ",".join(f"{key}={_escape_property(value)}" for key, value in properties.items())
            lines.append(f"::{finding.severity.value} {shown}::{_escape_data(message)}")
    return "".join(line + "\n" for line in lines)


FORMATS: dict[str, Callable[..., str]] = {
    "text": render_text,
    "json": render_json,
    "sarif": render_sarif,
    "github": render_github,
}

_RULE_DESCRIPTIONS = {
    unproved(kind): f"An asserted {kind.replace('-', ' ')} Property is not proved (UNKNOWN)" for kind in ASSERTED_KINDS.values()
}


def _place(finding: Finding) -> str:
    origin = finding.origin
    if origin is None or origin.line is None:
        return finding.path
    if origin.span is None:
        return f"{finding.path}:{origin.line}"
    return f"{finding.path}:{origin.line}:{origin.span.column}"


def _location(origin: Origin | None) -> dict | None:
    if origin is None or origin.line is None:
        return None
    if origin.span is None:
        return {"line": origin.line}
    span = origin.span
    return {"line": span.line, "column": span.column, "end_line": span.end_line, "end_column": span.end_column}


def _summary(checked: Sequence[Checked]) -> dict:
    assertions = [assertion for result in checked for assertion in result.report.assertions]
    found = [finding for result in checked for finding in result.findings]
    return {
        "files": len(checked),
        "assertions": len(assertions),
        "proved": sum(assertion.outcome is Outcome.PROVED for assertion in assertions),
        "unknown": sum(assertion.outcome is Outcome.UNKNOWN for assertion in assertions),
        "errors": sum(finding.severity is Severity.ERROR for finding in found),
        "warnings": sum(finding.severity is Severity.WARNING for finding in found),
    }


def _summary_line(checked: Sequence[Checked]) -> str:
    counts = _summary(checked)

    def plural(count: int, noun: str) -> str:
        return f"{count} {noun}{'' if count == 1 else 's'}"

    return (
        f"{plural(counts['files'], 'file')} checked: {plural(counts['assertions'], 'assertion')} "
        f"({counts['proved']} PROVED, {counts['unknown']} UNKNOWN), "
        f"{plural(counts['errors'], 'error')}, {plural(counts['warnings'], 'warning')}"
    )


def _uri(path: str) -> str:
    if os.path.isabs(path):
        return Path(path).as_uri()
    return quote(path)


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")
