"""The one-stable-model policy: `analyze` must never hand back a Report built
from zero or multiple models, because either one means the rule program (not
the SQL) has an unaccounted-for degree of freedom.

The deterministic-rule guard (`tests/test_clingo_rule_policy.py`) already keeps
`sqlassert/rules/*.lp` free of the ASP constructs that could cause this, so
there is no SQL program that reaches this failure through the real rules.
These tests substitute a tiny nondeterministic/unsatisfiable rule string for
the real ones -- the smallest possible stand-in for "the guard missed
something" -- to prove `analyze` itself, not just the guard, refuses to
produce a misleading Report when Clingo returns anything but exactly one
model.
"""

from __future__ import annotations

import pytest

from sqlassert import analyze
from sqlassert.engine import EnginePolicyError

# An odd loop: `a` is true only if `a` is false. No stable model satisfies it.
_ZERO_MODELS = "a :- not a."

# An unconstrained choice: both `{}` and `{a}` are stable models.
_TWO_MODELS = "{a}."


def test_zero_stable_models_raises_rather_than_reporting(monkeypatch):
    monkeypatch.setattr("sqlassert.engine.rules", lambda: _ZERO_MODELS)

    with pytest.raises(EnginePolicyError, match="none"):
        analyze("SELECT 1")


def test_multiple_stable_models_raises_rather_than_reporting(monkeypatch):
    monkeypatch.setattr("sqlassert.engine.rules", lambda: _TWO_MODELS)

    with pytest.raises(EnginePolicyError, match="more"):
        analyze("SELECT 1")
