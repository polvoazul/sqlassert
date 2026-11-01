"""Analysis-scoped memoization and evidence for Python property functions."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields
from functools import wraps
from inspect import signature

from case_conversion import snake

from sqlassert import ir
from sqlassert.diagnostics import DUPLICATE_DECLARATION, Diagnostic
from sqlassert.properties import Property
from sqlassert.reporting import AssertionReport, Outcome, RelationFacts, Report


@dataclass(eq=False)
class Result[T]:
    """A property function's value and evidence, including collection members."""

    value: T
    reason: str
    subjects: tuple[object, ...] = ()
    premises: list[Result] = field(default_factory=list)
    kind: str = "structural"
    members: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        raise TypeError("Use .value to inspect a result, or & and | to compose Boolean evidence")

    def __and__(self: Result[bool], other: Result[bool]) -> Result[bool]:
        return all_of((self, other))

    def __or__(self: Result[bool], other: Result[bool]) -> Result[bool]:
        return any_of((self, other))

    def explain(self) -> str:
        """Render the evidence, referring back to shared premises by number."""
        lines: list[str] = []
        seen: dict[Result, int] = {}

        def visit(result: Result, depth: int) -> None:
            indent = "  " * depth
            if result in seen:
                lines.append(f"{indent}see #{seen[result]}")
                return
            seen[result] = len(seen) + 1
            match result.value:
                case True:
                    state = "PROVED"
                case False:
                    state = "UNKNOWN"
                case _:
                    state = "VALUE"
            locations = []
            for subject in result.subjects:
                match subject:
                    case ir.Node(origin=origin):
                        locations.append(origin)
                    case _:
                        pass  # Property arguments may also be sets and other plain values.
            sources = "; ".join(
                f"{origin.detail}" + (f" (line {origin.line})" if origin.line is not None else "")
                for origin in dict.fromkeys(locations)
            )
            suffix = f" — {sources}" if sources else ""
            lines.append(f"{indent}#{seen[result]} {state}: {result.reason}{suffix}")
            for premise in result.premises:
                visit(premise, depth + 1)

        visit(self, 0)
        return "\n".join(lines)


def unknown(reason: str, *subjects: object) -> Result[bool]:
    return Result(False, reason, subjects, kind="unknown")


def all_of(results: Iterable[Result[bool]]) -> Result[bool]:
    premises = list(results)
    return Result(all(_boolean_values(premises)), "All premises required", premises=premises, kind="all")


def any_of(results: Iterable[Result[bool]]) -> Result[bool]:
    premises = list(results)
    return Result(any(_boolean_values(premises)), "Any alternative suffices", premises=premises, kind="any")


def _boolean_values(results):
    values = []
    for result in results:
        match result.value:
            case True | False:
                values.append(result.value)
            case _:
                raise TypeError("Boolean composition requires Result[bool]")
    return values


_active: ContextVar[Arena] = ContextVar("property_analysis")
analysis_functions = []


def cache(function):
    """Memoize only within the active analysis, using normalized arguments."""
    parameters = signature(function)

    @wraps(function)
    def cached(*args, **kwargs):
        arena = current_arena()
        bound = parameters.bind(*args, **kwargs)
        bound.apply_defaults()
        key = (function, tuple(bound.arguments.values()))
        if key in arena.cache:
            return arena.cache[key]
        if key in arena.evaluating:
            raise RuntimeError(f"cyclic property dependency: {cached.__name__}")
        arena.evaluating.add(key)
        try:
            result = function(*args, **kwargs)
            arena.cache[key] = result
            return result
        finally:
            arena.evaluating.remove(key)

    return cached


def analysis(function):
    """Cache and name an analysis function; public ones match Property names and fields."""
    parameters = signature(function)

    @cache
    @wraps(function)
    def evaluate(*args, **kwargs):
        bound = parameters.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = tuple(sorted(bound.arguments.items()))
        accepted = current_arena().accepted.get((function.__name__, arguments), ())
        result = any_of(accepted) if accepted else function(*args, **kwargs)
        return Result(result.value, function.__name__, tuple(bound.arguments.values()), [result], "claim", result.members)

    analysis_functions.append(evaluate)
    return evaluate


def current_arena() -> Arena:
    return _active.get()


class Arena:
    """One immutable program's knowledge, memoized values, and evidence graph."""

    def __init__(self, program: ir.Program, knowledge: tuple[Property, ...]):
        self.cache: dict[tuple, object] = {}
        self.evaluating: set[tuple] = set()
        self.accepted: dict[tuple, tuple[Result[bool], ...]] = {}
        self.program = program
        self.knowledge = (*knowledge, *program.declarations)
        # Schema Knowledge is accepted; Property Declarations are the author's trusted claims.
        sources = [("Accepted", item) for item in knowledge] + [("Declared", item) for item in program.declarations]
        for label, item in sources:
            values = tuple(getattr(item, field.name) for field in fields(item))
            subjects = []
            for value in values:
                match value:
                    case frozenset() | set() | list() | tuple():
                        subjects.extend(value)
                    case _:
                        subjects.append(value)
            evidence = Result(True, f"{label} {type(item).__name__}", (item, *subjects), kind="accepted")
            for cls in Property._subclasses:
                if cls not in type(item).__mro__:
                    continue
                arguments = tuple(sorted((field.name, getattr(item, field.name)) for field in fields(cls)))
                key = (snake(cls.__name__), arguments)
                self.accepted[key] = (*self.accepted.get(key, ()), evidence)

    def prove(self, property: Property) -> Result[bool]:
        values = {field.name: getattr(property, field.name) for field in fields(property)}
        evaluator, = (function for function in analysis_functions if function.__name__ == snake(type(property).__name__))
        return evaluator(**values)


class Engine:
    @contextmanager
    def session(self, program: ir.Program, knowledge: tuple[Property, ...] = ()) -> Iterator[Arena]:
        assert _active.get(None) is None, "An analysis arena is already active"
        # Importing registers analysis functions; inference remains in rules/.
        from sqlassert.rules import nullability, unique_join, uniqueness  # noqa: F401

        arena = Arena(program, knowledge)
        token = _active.set(arena)
        try:
            yield arena
        finally:
            _active.reset(token)

    def run(self, program: ir.Program, knowledge: tuple[Property, ...] = (), diagnostics: tuple[Diagnostic, ...] = ()) -> Report:
        from sqlassert.rules.uniqueness import _unique_sets

        with self.session(program, knowledge) as arena:
            assertions = []
            for index, assertion in enumerate(program.assertions, 1):
                evidence = arena.prove(assertion.property)
                if any(diagnostic.code == DUPLICATE_DECLARATION for diagnostic in diagnostics):
                    evidence = evidence & unknown("Duplicate declarations prevent accepting a proof", assertion)
                assertions.append(AssertionReport(
                    f"assert_{index}", Outcome.PROVED if evidence.value else Outcome.UNKNOWN, assertion.origin, evidence,
                ))
            facts = {}
            for relation in program.named_relations:
                keys = _unique_sets(relation).value
                facts[relation.report_name.lower()] = tuple(
                    tuple(column.name for column in relation.output_columns if column in key) for key in keys
                )
            return Report(tuple(assertions), tuple(diagnostics), RelationFacts(facts))
