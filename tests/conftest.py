"""The opt-in soundness oracle: `python -m pytest --soundness-oracle`.

See `soundness_oracle` for what it checks.
"""

from __future__ import annotations

import pytest

import soundness_oracle

_SUMMARY = pytest.StashKey[list[str]]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--soundness-oracle",
        action="store_true",
        help="record every analyze() call in the run, then judge each PROVED assertion against DuckDB",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "soundness_oracle: judges the run's PROVED assertions; runs last, only with --soundness-oracle"
    )
    config.stash[_SUMMARY] = []
    if config.getoption("--soundness-oracle"):
        soundness_oracle.record_analyze_calls()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    oracle = [item for item in items if item.get_closest_marker("soundness_oracle")]
    if not config.getoption("--soundness-oracle"):
        for item in oracle:
            item.add_marker(pytest.mark.skip(reason="opt-in: run with --soundness-oracle"))
        return
    # Last, so every other test has already recorded its programs.
    items[:] = [item for item in items if item not in oracle] + oracle


@pytest.fixture
def soundness_oracle_summary(request: pytest.FixtureRequest) -> list[str]:
    return request.config.stash[_SUMMARY]


def pytest_terminal_summary(terminalreporter, config: pytest.Config) -> None:
    summary = config.stash[_SUMMARY]
    if summary:
        terminalreporter.section("soundness oracle")
        for line in summary:
            terminalreporter.write_line(line)
