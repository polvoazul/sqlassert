"""Compare default Python analysis with Clingo in every end-to-end SQL scenario."""

import pytest

from sqlassert import analyze


@pytest.fixture(autouse=True)
def compare_python_engine(request, monkeypatch):
    if not request.module.__name__.split(".")[-1].startswith("test_analyze_"):
        return

    def compare(sql, **kwargs):
        python = analyze(sql, **kwargs)
        baseline = analyze(sql, **(kwargs | {"engine": "clingo"}))
        assert [(a.outcome, a.origin) for a in python.assertions] == [
            (a.outcome, a.origin) for a in baseline.assertions
        ]
        assert python.diagnostics == baseline.diagnostics
        assert python.proved == baseline.proved
        assert python.stable_model_count == 0
        assert baseline.stable_model_count == 1
        names = python.facts._unique_sets.keys() | baseline.facts._unique_sets.keys()
        for name in names:
            assert set(python.facts.unique_sets(name)) == set(baseline.facts.unique_sets(name))
        return python

    monkeypatch.setattr(request.module, "analyze", compare)
