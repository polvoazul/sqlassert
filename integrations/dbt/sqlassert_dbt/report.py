"""What analyzing a dbt project found, as plain values, and its text rendering.

Every item has a `Location` (a project-relative path and a line when known),
so each maps onto a `check` finding: a path, a line, a code, a message, and a
severity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from sqlassert.check import EXIT_FINDINGS, EXIT_OK
from sqlassert.reporting import Outcome
from sqlassert.rewrites import SuggestType


@dataclass(frozen=True)
class Location:
    path: str
    line: int | None = None

    def __str__(self) -> str:
        return self.path if self.line is None else f"{self.path}:{self.line}"


class Status(Enum):
    PROVED = "proved"
    NOT_PROVED = "not proved"


@dataclass(frozen=True)
class TestOutcome:
    """A dbt test or contract constraint, checked against its model's SQL.

    A PROVED test can never fail, so dbt may skip it; a NOT_PROVED one still
    carries coverage only the warehouse run gives. `reason` says why it was
    not proved; `explanation` is the proof or the failed attempt.
    """

    unique_id: str
    name: str
    kind: Literal["test", "constraint"]
    model: str
    location: Location
    status: Status
    reason: str = ""
    explanation: str = ""

    @property
    def skippable(self) -> bool:
        return self.kind == "test" and self.status is Status.PROVED


@dataclass(frozen=True)
class MarkerOutcome:
    """A Property Marker written in a model file. `location` is the model file
    when its line is known, else the compiled file and its line."""

    model: str
    location: Location
    marker: str
    outcome: Outcome
    explanation: str = ""


@dataclass(frozen=True)
class Untested:
    """A Property proved of a model that none of its tests states: `test` is
    the entry to add under the column (or, with no column, the model) in `location`."""

    model: str
    location: Location
    column: str | None
    test: str
    explanation: str = ""


@dataclass(frozen=True)
class Problem:
    """A Diagnostic: a part of the project analysis could not model. A warning
    names a fact a connection or the project could supply; it never fails. An
    error fails the check: a model whose own markers went unchecked."""

    code: str
    message: str
    severity: Literal["error", "warning"]
    location: Location | None = None
    model: str | None = None


@dataclass(frozen=True)
class DbtReport:
    dialect: str | None
    tests: tuple[TestOutcome, ...] = ()
    markers: tuple[MarkerOutcome, ...] = ()
    untested: tuple[Untested, ...] = ()
    problems: tuple[Problem, ...] = ()

    @property
    def failed(self) -> bool:
        """A marker not proved, or an error. Tests not proved do not fail it:
        the warehouse run still checks them."""
        return any(marker.outcome is Outcome.UNKNOWN for marker in self.markers) or any(
            problem.severity == "error" for problem in self.problems
        )


@dataclass(frozen=True)
class ModelSuggestion:
    """What `sqlassert suggest` finds in a model's SQL, by `suggest_type`: a
    rewrite a proof justifies (with a caveat when a column's type changes), a marker that will be PROVED, or a hazard: a
    result the SQL leaves unspecified. `location` is the model file's line when
    known, else the compiled file's; `kind` names the rewrite, Property or
    concern; `text` is the marker to paste or the message; `diff` is a
    rewrite's unified diff against the model's compiled SQL."""

    model: str
    location: Location
    suggest_type: str
    label: str
    kind: str
    text: str
    explanation: str = ""
    diff: str = ""


@dataclass(frozen=True)
class SuggestReport:
    """`models` names every model analyzed, with suggestions or none."""

    dialect: str | None
    models: tuple[str, ...] = ()
    suggestions: tuple[ModelSuggestion, ...] = ()
    problems: tuple[Problem, ...] = ()

    @property
    def failed(self) -> bool:
        return any(problem.severity == "error" for problem in self.problems)


# How many warnings of one code are printed before the rest are suppressed.
DEFAULT_WARNING_LIMIT = 10


@dataclass
class WarningLimit:
    """Prints at most `limit` warnings of each code (0: every one), then one
    line saying the rest are suppressed. Errors are always printed. One
    instance spans every block of a streamed listing."""

    limit: int = DEFAULT_WARNING_LIMIT
    seen: dict[str, int] = field(default_factory=dict)

    def lines(self, problem: Problem) -> list[str]:
        where = f"{problem.location}: " if problem.location is not None else ""
        line = f"{where}{problem.severity} {problem.code}: {problem.message}"
        if problem.severity == "error" or not self.limit:
            return [line]
        count = self.seen[problem.code] = self.seen.get(problem.code, 0) + 1
        if count <= self.limit:
            return [line]
        if count == self.limit + 1:
            return [f"warning: suppressing further '{problem.code}' warnings (--warning-limit 0 prints every one)"]
        return []

    @property
    def suppressed(self) -> int:
        return sum(max(0, count - self.limit) for count in self.seen.values()) if self.limit else 0


def format_suggestions(report: SuggestReport, *, explain: bool = False) -> str:
    """The models' blocks, then the summary."""
    limit = WarningLimit()
    return format_suggestion_blocks(report, explain=explain, limit=limit) + format_suggestion_summary(report, limit)


def format_suggestion_blocks(report: SuggestReport, *, explain: bool = False, limit: WarningLimit | None = None) -> str:
    """One block per model, each suggestion one aligned row (a rewrite's diff
    against the compiled SQL under it), then the report's warnings and
    errors; each block ends in a blank line, so blocks printed as models
    finish read as one listing."""
    lines: list[str] = []
    for model in report.models:
        items = [item for item in report.suggestions if item.model == model]
        if not items:
            lines.append(f"{model}: nothing to suggest")
            continue
        rows = [(str(item.location), item.suggest_type, item.label, item.kind, item.text) for item in items]
        widths = [max(len(row[i]) for row in rows) for i in range(4)]
        lines.append(f"{model}:")
        for row, item in zip(rows, items):
            lines.append("  " + "  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths)) + "  " + row[4])
            lines.extend("      " + line for line in item.diff.splitlines())
            if explain and item.explanation:
                lines.extend("      " + line for line in item.explanation.splitlines())
    limit = limit or WarningLimit(0)
    for problem in report.problems:
        lines.extend(limit.lines(problem))
    return "".join(line + "\n" for line in lines) + ("\n" if lines else "")


def format_suggestion_summary(report: SuggestReport, limit: WarningLimit | None = None) -> str:
    counts = {kind: sum(item.suggest_type == kind.value for item in report.suggestions) for kind in SuggestType}
    return (
        f"{counts[SuggestType.REWRITE]} rewrites, {counts[SuggestType.REWRITE_WITH_CAVEAT]} rewrites with caveat, "
        f"{counts[SuggestType.MARKER]} markers, {counts[SuggestType.HAZARD]} hazards in {len(report.models)} models, "
        f"{sum(p.severity == 'error' for p in report.problems)} errors, "
        f"{sum(p.severity == 'warning' for p in report.problems)} warnings{_suppressed(limit)}\n"
    )


def _suppressed(limit: WarningLimit | None) -> str:
    return f" ({limit.suppressed} suppressed)" if limit is not None and limit.suppressed else ""


def exit_code(report: DbtReport, *, require_proved: bool = False) -> int:
    """`EXIT_FINDINGS` when the report failed or, under `require_proved`, a
    test is not proved: for a project whose every test the SQL must back."""
    unproved = require_proved and any(test.kind == "test" and test.status is Status.NOT_PROVED for test in report.tests)
    return EXIT_FINDINGS if report.failed or unproved else EXIT_OK


def format_text(report: DbtReport, *, explain: bool = False, warning_limit: int = DEFAULT_WARNING_LIMIT) -> str:
    lines: list[str] = []
    limit = WarningLimit(warning_limit)

    def evidence(text: str) -> None:
        if explain and text:
            lines.extend("    " + line for line in text.splitlines())

    for marker in report.markers:
        lines.append(f"{marker.location}: {marker.outcome.value.upper()} {marker.marker} ({marker.model})")
        if marker.outcome is Outcome.UNKNOWN or explain:
            evidence(marker.explanation)
    for problem in report.problems:
        lines.extend(limit.lines(problem))
    proved = [test for test in report.tests if test.status is Status.PROVED]
    unproved = [test for test in report.tests if test.status is Status.NOT_PROVED]
    if proved:
        lines.append("")
        lines.append("Proved statically (a test here can be skipped):")
        for test in proved:
            lines.append(f"  {test.location}  {test.name}  ({test.kind} on {test.model})")
            evidence(test.explanation)
    if unproved:
        lines.append("")
        lines.append("Not proved (the warehouse run still checks these):")
        for test in unproved:
            lines.append(f"  {test.location}  {test.name}  ({test.kind} on {test.model}): {test.reason}")
            evidence(test.explanation)
    if report.untested:
        lines.append("")
        lines.append("Proved but untested (schema.yml additions):")
        for item in report.untested:
            target = f"{item.model}.{item.column}" if item.column else item.model
            lines.append(f"  {item.location}  {target}: {item.test}")
            evidence(item.explanation)
    markers_unknown = sum(marker.outcome is Outcome.UNKNOWN for marker in report.markers)
    lines.append("")
    lines.append(
        f"{len(proved)} proved, {len(unproved)} not proved, {len(report.untested)} untested properties, "
        f"{len(report.markers)} markers ({markers_unknown} unknown), "
        f"{sum(p.severity == 'error' for p in report.problems)} errors, {sum(p.severity == 'warning' for p in report.problems)} warnings"
        f"{_suppressed(limit)}"
    )
    return "\n".join(lines) + "\n"
