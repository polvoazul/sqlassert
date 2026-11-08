"""The opt-in, per-test soundness oracle: `python -m pytest --soundness-oracle`."""

from __future__ import annotations

from collections import Counter

import pytest

import soundness_oracle
from sqltest import SqlTestFile


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--soundness-oracle",
        action="store_true",
        help="judge each test's PROVED assertions against DuckDB after its fixtures finish",
    )


def pytest_collect_file(file_path, parent):
    if file_path.suffix == ".sqltest":
        return SqlTestFile.from_parent(parent, path=file_path)
    return None


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--soundness-oracle"):
        soundness_oracle.record_analyze_calls()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.config.getoption("--soundness-oracle"):
        # Calls made during collection do not belong to this test.
        soundness_oracle.take_recorded_programs()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item):
    result = yield
    if not item.config.getoption("--soundness-oracle"):
        return result
    # All fixture finalizers, including monkeypatch restoration, have finished.
    programs = soundness_oracle.take_recorded_programs()
    if not programs:
        return result

    counts = Counter(programs=len(programs))
    details: list[str] = []
    found: list[str] = []
    for program in programs:
        judgement = soundness_oracle.judge(program.sql, program.report, dialect=program.dialect)
        counts["checked"] += judgement.checked
        counts["skipped"] += len(judgement.skipped)
        counts["valid"] += sum(search.valid for _, search in judgement.searches)
        counts["rejected"] += sum(search.rejected for _, search in judgement.searches)
        details.extend(f"skipped {item.nodeid}: {reason}" for reason in judgement.skipped)
        for counterexample in judgement.counterexamples:
            known = soundness_oracle.KNOWN_UNSOUND.get((item.nodeid, counterexample.assertion_id))
            if known:
                counts["known"] += 1
                details.append(f"known finding {item.nodeid} ({known})\n{counterexample.explain()}")
            else:
                counts["counterexamples"] += 1
                found.append(counterexample.explain())

    # Pytest carries these plain values to the xdist controller with this report.
    item.user_properties.append(("soundness_oracle", (dict(counts), details)))
    if found:
        pytest.fail("PROVED assertions with counterexamples:\n\n" + "\n\n".join(found), pytrace=False)
    return result


def pytest_terminal_summary(terminalreporter, config: pytest.Config) -> None:
    if not config.getoption("--soundness-oracle"):
        return
    counts: Counter[str] = Counter()
    details: list[str] = []
    for reports in terminalreporter.stats.values():
        for report in reports:
            if not isinstance(report, pytest.TestReport) or report.when != "teardown":
                continue
            for name, data in report.user_properties:
                if name == "soundness_oracle":
                    summary, lines = data
                    counts.update(summary)
                    details.extend(lines)
    if not counts:
        return
    terminalreporter.section("soundness oracle")
    terminalreporter.write_line(
        f"{counts['programs']} programs, {counts['checked']} PROVED assertions judged, "
        f"{counts['counterexamples']} counterexamples, {counts['known']} known findings, {counts['skipped']} skipped; "
        f"{counts['valid']} valid samples, {counts['rejected']} rejected (including shrinking)"
    )
    for line in sorted(details):
        terminalreporter.write_line(line)
