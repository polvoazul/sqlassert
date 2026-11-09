"""What analyzing a dbt project found, as plain values, and its text rendering.

Every item has a `Location` (a project-relative path and a line when known),
so each maps onto a `check` finding: a path, a line, a code, a message, and a
severity.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from sqlassert.check import EXIT_FINDINGS, EXIT_OK
from sqlassert.reporting import Outcome


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


def exit_code(report: DbtReport, *, require_proved: bool = False) -> int:
    """`EXIT_FINDINGS` when the report failed or, under `require_proved`, a
    test is not proved: for a project whose every test the SQL must back."""
    unproved = require_proved and any(test.kind == "test" and test.status is Status.NOT_PROVED for test in report.tests)
    return EXIT_FINDINGS if report.failed or unproved else EXIT_OK


def format_text(report: DbtReport, *, explain: bool = False) -> str:
    lines: list[str] = []

    def evidence(text: str) -> None:
        if explain and text:
            lines.extend("    " + line for line in text.splitlines())

    for marker in report.markers:
        lines.append(f"{marker.location}: {marker.outcome.value.upper()} {marker.marker} ({marker.model})")
        if marker.outcome is Outcome.UNKNOWN or explain:
            evidence(marker.explanation)
    for problem in report.problems:
        where = f"{problem.location}: " if problem.location is not None else ""
        lines.append(f"{where}{problem.severity} {problem.code}: {problem.message}")
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
    )
    return "\n".join(lines) + "\n"
