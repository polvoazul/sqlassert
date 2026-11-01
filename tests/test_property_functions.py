from dataclasses import fields
from inspect import Parameter, signature

from case_conversion import snake

from sqlassert.properties import Property
from sqlassert.python_engine import analysis_functions
from sqlassert.rules import exactly_one, foreign_key, nullability, unique_join, uniqueness  # noqa: F401 — register the rule functions


def test_every_property_has_exactly_one_matching_evaluator():
    for property_type in Property._subclasses:
        name = snake(property_type.__name__)
        evaluators = [function for function in analysis_functions if function.__name__ == name]
        assert len(evaluators) == 1, f"{property_type.__name__} needs exactly one {name} evaluator"
        parameters = signature(evaluators[0]).parameters
        assert tuple(parameters) == tuple(field.name for field in fields(property_type)), name
        assert all(p.kind in (Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY) for p in parameters.values()), name
