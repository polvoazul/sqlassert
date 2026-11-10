"""One dbt model as one SQL Program, analyzed once per `is_incremental()` branch.

The Program puts the model's SQL first, as a FROM subquery of a Root Select,
on the Program's own first line, so a line of the model's SQL is the same line
of the Program. The model's tests follow as Assertion Markers on that Root
Select, one per line. Every relation the model reads (and every relation its
relationships tests reference) is declared after it: a model whose rows are
its query (a view, a table rebuilt each run) as a `CREATE VIEW` of its compiled
SQL with its own assertions blanked out, anything else (a source, seed,
snapshot, incremental model) as a table with unknown columns, or with a
`CREATE TABLE` when its columns are known completely. Tests of those relations
that are facts become Property Declarations, bound to the IR after linking.

Nothing here writes a marker the author did not: the generated markers live on
the generated Root Select only, so the Property Markers stay the one
user-facing contract.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from sqlassert import diagnostics as diag
from sqlassert import ir
from sqlassert_dbt.expectations import Expectation, PropertyKind
from sqlassert_dbt.jinja import Rendered, Unrenderable, has_incremental_branch, render
from sqlassert_dbt.manifest import Column, Relation
from sqlassert.diagnostics import Diagnostic
from sqlassert.improvements import Improvement, Level
from sqlassert.ir.convert import IrConversionResult, IrParser
from sqlassert.ir.model import LiteralValue
from sqlassert.properties import (
    EnumeratedDomain, ForeignKey, NonNullColumn, NonNullColumns, NonNullUniqueSet, Property, ReferentialCoverage,
    UniqueSet,
)
from sqlassert.python_engine import Engine
from sqlassert.reporting import Outcome, Report
from sqlassert.rules.domain import _domain
from sqlassert.rules.equality import _binary_text, _comparison_type, _identity_comparison
from sqlassert.rules.uniqueness import minimal_keys
from sqlassert.smells import Smell
from sqlassert.sql_parse import ParsedProgram, SqlParser, _marker_shapes
from sqlassert.suggest import MAX_ENUM_VALUES, Suggester, Suggestion

# Materializations whose relation holds exactly the rows of the model's query
# when its children are built. An incremental model's table keeps rows of
# earlier runs, a snapshot's its history: those are tables, not their query.
QUERY_MATERIALIZATIONS = {"view", "table", "materialized_view", "dynamic_table"}

# Parse results that leave a relation's SQL unusable as a view body.
_UNPARSEABLE = {diag.SQL_PARSE_FAILED, diag.MULTIPLE_ROOT_SELECTS, diag.UNSUPPORTED_STATEMENT, diag.UNATTACHED_MARKER}

_CTE_NAME = re.compile(r"\s*(\S+)\s+as\s*\(", re.IGNORECASE)


@dataclass(frozen=True)
class Body:
    """A model's SQL as analyzed, and where its lines came from.

    `lines` holds, for each character, the line of the model file it was
    copied from (0 where a tag produced it), when a rendering of the raw code
    reproduced dbt's; None when none did, and markers are then found in the
    raw code by their text. `injected` holds the spans dbt inserted for
    ephemeral models: their findings belong to those models.
    """

    text: str
    raw_code: str
    lines: tuple[int, ...] | None = None
    injected: tuple[tuple[int, int], ...] = ()

    def in_injected(self, offset: int) -> bool:
        return any(start <= offset < end for start, end in self.injected)


@dataclass(frozen=True)
class Branches:
    """The bodies of every branch dbt may run; `unrendered` says why a branch is missing."""

    bodies: tuple[Body, ...]
    unrendered: str | None = None
    warnings: tuple[str, ...] = ()


def branches(model: Relation, relations: Mapping[str, Relation]) -> Branches:
    """The compiled body, and for an incremental model the other branch too."""
    compiled = _strip_end(model.compiled_code or "")
    joined = ", ".join(cte.sql for cte in model.extra_ctes)
    warnings: list[str] = []
    original, cut = (compiled, None) if not joined else _uninject(compiled, joined)
    if original is None:
        warnings.append(
            "dbt's inlined ephemeral models could not be located in its compiled code, so their markers are analyzed as this model's"
        )
    injected = () if cut is None or not joined else ((cut[0], cut[0] + len(joined)),)
    two = model.materialized == "incremental" and has_incremental_branch(model.raw_code)
    renderings: dict[bool, Rendered] = {}
    failure: str | None = None
    for incremental in (False, True) if two else (False,):
        try:
            renderings[incremental] = render(model.raw_code, incremental=incremental, resolve=_resolver(model, relations))
        except Unrenderable as error:
            failure = str(error)
    taken = next(
        (flag for flag, rendered in renderings.items() if original is not None and _strip_end(rendered.text) == original),
        None,
    )
    lines = None
    if taken is not None:
        lines = _raw_lines(model.raw_code, _compiled_sources(renderings[taken], len(compiled), cut, len(joined)))
    first = Body(compiled, model.raw_code, lines, injected)
    if not two:
        return Branches((first,), None, tuple(warnings))
    if taken is None or (not taken) not in renderings:
        reason = failure or "rendering the raw code did not reproduce dbt's compiled code"
        return Branches((first,), f"the other is_incremental() branch was not analyzed: {reason}", tuple(warnings))
    return Branches((first, _inject(renderings[not taken], joined, model.raw_code)), None, tuple(warnings))


def _raw_lines(raw: str, sources: Iterable[int]) -> tuple[int, ...]:
    """Each raw offset as its 1-based line, -1 as 0."""
    starts = [0] + [index + 1 for index, char in enumerate(raw) if char == "\n"]
    return tuple(0 if source < 0 else bisect_right(starts, source) for source in sources)


def _strip_end(sql: str) -> str:
    """Without trailing whitespace and semicolons, which the Program supplies."""
    return re.sub(r"[\s;]*\Z", "", sql)


def _uninject(compiled: str, joined: str) -> tuple[str | None, tuple[int, int] | None]:
    """The code dbt rendered before inlining ephemeral models, and the cut it made.

    dbt inserts the joined CTE text right after the WITH (or RECURSIVE) of the
    query, followed by a comma; with no WITH, it inserts `with` and the CTEs
    before the first token. Returns the original and (offset of the CTE text,
    characters of `with` or `,` added around it)."""
    index = compiled.find(joined)
    if index < 0 or compiled.find(joined, index + 1) >= 0:
        return None, None
    after = index + len(joined)
    if compiled.startswith(",", after):
        return compiled[:index] + compiled[after + 1:], (index, 0)
    if compiled[max(0, index - 4):index] == "with":
        return compiled[:index - 4] + compiled[after:], (index, 4)
    return None, None


def _compiled_sources(rendered: Rendered, length: int, cut: tuple[int, int] | None, injected: int) -> tuple[int, ...]:
    """Each compiled character's raw offset, through the cut dbt made."""
    sources: list[int] = []
    for offset in range(length):
        if cut is None:
            original = offset
        else:
            index, added = cut
            if offset < index - added:
                original = offset
            elif offset < index + injected + (1 if added == 0 else 0):
                original = -1
            else:
                original = offset - injected - (1 if added == 0 else 4)
        raw = rendered.raw_offset(original) if original >= 0 else None
        sources.append(-1 if raw is None else raw)
    return tuple(sources)


def _inject(rendered: Rendered, joined: str, raw_code: str) -> Body:
    """`rendered` with the ephemeral CTEs inlined as dbt would."""
    text = _strip_end(rendered.text)
    sources = list(rendered.sources[:len(text)])
    if not joined:
        return Body(text, raw_code, _raw_lines(raw_code, sources))
    try:
        tokens = sqlglot.tokenize(text)
    except SqlglotError:
        tokens = []
    if tokens and tokens[0].token_type is TokenType.WITH:
        end = tokens[1].end + 1 if len(tokens) > 1 and tokens[1].token_type is TokenType.RECURSIVE else tokens[0].end + 1
        insert, at = joined + ",", end
    else:
        insert, at = "with" + joined + "\n", 0
    return Body(
        text[:at] + insert + text[at:], raw_code, _raw_lines(raw_code, sources[:at] + [-1] * len(insert) + sources[at:]),
        ((at, at + len(insert)),),
    )


def _resolver(model: Relation, relations: Mapping[str, Relation]):
    from sqlassert_dbt.expectations import resolve

    def resolved(function: str, arguments: tuple[object, ...], options: dict[str, object]) -> str:
        if function == "this":
            if model.relation_name is None:
                raise Unrenderable("{{ this }} of a model with no relation")
            return model.relation_name
        found = resolve(function, tuple(str(argument) for argument in arguments), options, model.depends_on, relations)
        if found is None:
            raise Unrenderable(f"{function}{arguments} does not resolve among the model's dependencies")
        target = relations[found]
        if target.materialized == "ephemeral":
            name = next(
                (match.group(1) for cte in model.extra_ctes if cte.id == found and (match := _CTE_NAME.match(cte.sql))), None,
            )
            if name is None:
                raise Unrenderable(f"the ephemeral model {target.name} is not inlined")
            return name
        if target.relation_name is None:
            raise Unrenderable(f"{target.name} has no relation name")
        return target.relation_name

    return resolved


# Programs ---------------------------------------------------------------


@dataclass(frozen=True)
class TestResult:
    proved: bool
    explanation: str


@dataclass(frozen=True)
class MarkerResult:
    """One of the model's own markers in one branch. `line` is the body line;
    `raw_line` the model file's line, when known; `place` its position among
    the markers on its line."""

    line: int
    raw_line: int | None
    place: int
    marker: str
    outcome: Outcome
    explanation: str


@dataclass(frozen=True)
class Problem:
    """A Diagnostic of the model's SQL, `line` in its body (None: no line), `raw_line` in its file."""

    diagnostic: Diagnostic
    warning: bool
    line: int | None
    raw_line: int | None


@dataclass(frozen=True)
class Addition:
    """A Property proved of the model and tested by nothing: `kind` and its columns (and values)."""

    kind: PropertyKind
    columns: tuple[str, ...]
    values: tuple[ir.LiteralValue, ...] = ()
    explanation: str = ""


@dataclass(frozen=True)
class BranchResult:
    tests: dict[str, TestResult] = field(default_factory=dict)
    markers: tuple[MarkerResult, ...] = ()
    problems: tuple[Problem, ...] = ()
    additions: tuple[Addition, ...] = ()
    # Relations reached that are tables with unknown columns.
    undeclared: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Suggested:
    """An Improvement, marker or Smell `sqlassert suggest` finds in a model's
    SQL: `line` in its body, `raw_line` in its file when known; `kind` the
    rewrite, Property or concern; `text` the marker to paste or the message."""

    level: Level
    line: int
    raw_line: int | None
    label: str
    kind: str
    text: str
    explanation: str
    trailing: bool = False


@dataclass(frozen=True)
class SuggestResult:
    suggested: tuple[Suggested, ...] = ()
    problems: tuple[Problem, ...] = ()
    undeclared: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _Program:
    """A model's Program, parsed and lowered with the facts of what it reads
    declared; `placed` maps a line to the test whose marker it holds."""

    sql: str
    ast: ParsedProgram
    conversion: IrConversionResult
    reached: list[Relation]
    body_lines: int
    placed: dict[int, Expectation]
    unplaced: dict[str, TestResult]
    undeclared: frozenset[str]


@dataclass(frozen=True)
class _Stated:
    """What a model's tests and constraints state of its rows, by lowercase column names."""

    non_null: frozenset[str]
    unique: frozenset[frozenset[str]]
    enums: frozenset[tuple[str, frozenset[LiteralValue]]]

    @staticmethod
    def of(own: Iterable[Expectation]) -> _Stated:
        stating = [e for e in own if e.asserted is not None and e.unprovable is None]
        return _Stated(
            frozenset(c.lower() for e in stating if e.asserted in {"not_null", "key"} for c in e.columns),
            frozenset(frozenset(c.lower() for c in e.columns) for e in stating if e.asserted in {"unique", "key"}),
            frozenset((e.columns[0].lower(), frozenset(e.values)) for e in stating if e.asserted == "enum"),
        )

    def untested(
        self, property: Property, marker: str, order: Iterable[ir.OutputColumn], dialect: str,
    ) -> str | None:
        """`marker`, or the part of it no test states (None: they state it all)."""
        match property:
            case NonNullColumns(columns=columns) | NonNullUniqueSet(columns=columns):
                if isinstance(property, NonNullUniqueSet) and _names(columns) not in self.unique:
                    return marker
                rest = [column for column in order if column in columns and column.name.lower() not in self.non_null]
                if isinstance(property, NonNullColumns) and len(rest) == len(columns):
                    return marker
                return f"/**ASSERT NOT NULL({', '.join(_identifier(c.name, dialect) for c in rest)})**/" if rest else None
            case UniqueSet(columns=columns):
                return None if _names(columns) in self.unique else marker
            case EnumeratedDomain(column=column, values=values):
                return None if (column.name.lower(), frozenset(values)) in self.enums else marker
            case _:
                return marker


def _names(columns: Iterable[ir.OutputColumn]) -> frozenset[str]:
    return frozenset(column.name.lower() for column in columns)


# The generated Root Select reads the model's body as a FROM subquery,
# starting on the Program's first line.
_PREFIX = "select * from ("


@dataclass
class Builder:
    """Builds and analyzes the Programs of one project. Bodies of other
    relations are parsed once and reused for every model that reads them."""

    relations: Mapping[str, Relation]
    expectations: tuple[Expectation, ...]
    dialect: str
    # Whether the tests of what a model reads passed before it was built, as
    # under `dbt build`. Plain `dbt run` runs no test, so by default only
    # enforced contract constraints are facts.
    trust_upstream_tests: bool = False
    # The upstream tests that would have been facts had they been trusted.
    untrusted: set[str] = field(default_factory=set)
    # Every column of a relation, in order, from catalog.json (`--catalog`):
    # used for a table no enforced contract types completely.
    catalog_columns: Mapping[str, tuple[Column, ...]] = field(default_factory=dict)
    _usable: dict[str, bool] = field(default_factory=dict)

    def facts(self, node: str) -> list[Expectation]:
        found = [e for e in self.expectations if e.node == node and e.is_fact]
        if self.trust_upstream_tests:
            return found
        self.untrusted.update(e.unique_id for e in found if e.kind == "test")
        return [e for e in found if e.kind == "constraint"]

    def is_view(self, relation: Relation) -> bool:
        """Whether `relation` enters a Program as its query rather than as a table."""
        if relation.resource_type != "model" or relation.language != "sql" or relation.compiled_code is None:
            return False
        if relation.materialized not in QUERY_MATERIALIZATIONS | {"ephemeral"}:
            return False
        if relation.unique_id not in self._usable:
            parsed = SqlParser(self.dialect).parse(_blank_assertions(_strip_end(relation.compiled_code)))
            self._usable[relation.unique_id] = parsed.root_select is not None and not any(
                d.code in _UNPARSEABLE for d in parsed.diagnostics
            )
        return self._usable[relation.unique_id]

    def reached(self, model: Relation, tests: Iterable[Expectation]) -> list[Relation]:
        """Every relation the model's Program needs: what it reads, through
        views and ephemeral models, and what its tests reference."""
        pending = [*model.depends_on, *(e.referenced for e in tests if e.referenced is not None)]
        seen: dict[str, Relation] = {}
        while pending:
            node = pending.pop()
            if node in seen or node == model.unique_id or node not in self.relations:
                continue
            relation = self.relations[node]
            seen[node] = relation
            if self.is_view(relation):
                pending.extend(relation.depends_on)
        return list(seen.values())

    def _program(self, model: Relation, body: Body, tests: tuple[Expectation, ...]) -> _Program:
        """The model's Program: its body under a Root Select carrying `tests`
        as markers, then every relation it reaches, with their facts declared."""
        reached = self.reached(model, tests)
        body_lines = body.text.count("\n") + 1
        lines = [f"{_PREFIX}{body.text}", f") as {_identifier(model.name, self.dialect)}"]
        placed: dict[int, Expectation] = {}
        unplaced: dict[str, TestResult] = {}
        for expectation in tests:
            marker = self._marker(expectation)
            if marker is None:
                unplaced[expectation.unique_id] = TestResult(False, "it references a relation with no name in the warehouse")
                continue
            placed[body_lines + 1 + len(placed) + 1] = expectation
            lines.append(marker)
        lines.append(";")
        undeclared: set[str] = set()
        for relation in reached:
            statement = self._declaration(relation)
            if statement is None and relation.materialized != "ephemeral":
                undeclared.add(relation.unique_id)
            if statement:
                lines.append(statement)
        sql = "\n".join(lines) + "\n"

        ast = SqlParser(self.dialect).parse(sql)
        conversion = IrParser(self.dialect).parse(ast)
        program = conversion.program
        assertions = tuple(
            _coverage(assertion) if assertion.origin.line in placed and isinstance(assertion.property, ForeignKey) else assertion
            for assertion in program.assertions
        )
        declarations = (*program.declarations, *self._declarations(program, conversion.knowledge, model, reached))
        conversion = replace(conversion, program=replace(program, assertions=assertions, declarations=declarations))
        return _Program(sql, ast, conversion, reached, body_lines, placed, unplaced, frozenset(undeclared))

    def analyze(self, model: Relation, body: Body, tests: tuple[Expectation, ...]) -> BranchResult:
        built = self._program(model, body, tests)
        program, knowledge = built.conversion.program, built.conversion.knowledge
        diagnostics = built.ast.diagnostics + built.conversion.diagnostics
        report = Engine().run(program, knowledge, diagnostics, built.conversion.warnings)
        return BranchResult(
            tests={**built.unplaced, **_tests(report, built.placed)},
            markers=_markers(report, body, built.body_lines),
            problems=_problems(
                report.diagnostics, report.warnings, body, built.body_lines, built.placed, [model, *built.reached], self.dialect,
            ),
            additions=self._additions(program, knowledge, diagnostics, model),
            undeclared=built.undeclared,
        )

    def suggest(self, model: Relation, body: Body, own: Iterable[Expectation] = ()) -> SuggestResult:
        """What `sqlassert suggest` finds in the model's own SQL, with what it
        reads declared. A marker of its Root Select that `own` (the model's
        tests and constraints) already states is left out, or cut to the part
        none states."""
        built = self._program(model, body, ())
        # Only the model's own SQL: what it reads is suggested on in its own run.
        with Suggester(
            built.sql, built.ast, built.conversion, dialect=self.dialect,
            sites=lambda site: site.line <= built.body_lines and 0 <= site.offset - len(_PREFIX) <= len(body.text),
        ) as suggester:
            report = suggester.suggest()
        root = f"subquery {model.name}".lower()
        stated = _Stated.of(own)
        relations = dict(built.conversion.program.attachment_sites)
        found = []
        for item in (*report.improvements, *report.suggestions, *report.smells):
            site = item.site
            cut = False
            if isinstance(item, Suggestion) and site.label.lower() == root:
                relation = relations[site]
                order = () if isinstance(relation, ir.SimpleCase) else relation.output_columns
                marker = stated.untested(item.property, item.marker, order, self.dialect)
                if marker is None:
                    continue
                item, cut = replace(item, marker=marker), marker != item.marker
            offset = site.offset - len(_PREFIX)
            if site.line > built.body_lines or not 0 <= offset <= len(body.text):
                continue  # The generated Root Select, or a relation the model reads.
            # A trailing marker follows the Select Expression's last character,
            # or the last one copied from the model file when a tag wrote it.
            anchor = max(offset - 1, 0) if site.trailing else offset
            while site.trailing and body.lines is not None and anchor > 0 and not body.lines[anchor]:
                anchor -= 1
            if body.in_injected(anchor):
                continue
            match item:
                case Improvement():
                    kind, text = item.rewrite.value, item.message
                case Suggestion():
                    kind, text = "Not Null" if cut else item.kind, item.marker
                case Smell():
                    kind, text = item.concern.value, item.message
            found.append(Suggested(
                item.level, site.line, _site_line(body, site.line, anchor, site.trailing),
                "Root Select" if site.label.lower() == root else site.label, kind, text, item.evidence.explain(), site.trailing,
            ))
        return SuggestResult(
            tuple(found),
            _problems(report.diagnostics, report.warnings, body, built.body_lines, {}, [model, *built.reached], self.dialect),
            built.undeclared,
        )

    def _marker(self, expectation: Expectation) -> str | None:
        columns = ", ".join(_identifier(column, self.dialect) for column in expectation.columns)
        match expectation.asserted:
            case "unique":
                return f"/**ASSERT UNIQUE({columns})**/"
            case "key":
                return f"/**ASSERT NOT NULL UNIQUE({columns})**/"
            case "not_null":
                return f"/**ASSERT NOT NULL({columns})**/"
            case "enum":
                return f"/**ASSERT ENUM({columns} IN ({', '.join(value.sql() for value in expectation.values)}))**/"
            case "coverage":
                target = self.relations.get(expectation.referenced or "")
                if target is None or target.relation_name is None:
                    return None
                referenced = ", ".join(_identifier(column, self.dialect) for column in expectation.referenced_columns)
                return f"/**ASSERT FOREIGN KEY({columns}) REFERENCES {target.relation_name}({referenced})**/"
            case None:
                return None

    def _declaration(self, relation: Relation) -> str | None:
        if relation.relation_name is None:
            return None
        if self.is_view(relation):
            if relation.materialized == "ephemeral":
                return None  # dbt inlined it as a CTE wherever it is read.
            body = _blank_assertions(_strip_end(relation.compiled_code or ""))
            return f"create view {relation.relation_name} as\n{body}\n;"
        columns = self._complete_columns(relation)
        if columns is None:
            return None
        return f"create table {relation.relation_name} ({', '.join(columns)});"

    def _complete_columns(self, relation: Relation) -> list[str] | None:
        """Column definitions known to be all of a table's columns: an enforced
        contract's, typed, else the catalog's, typed where SQLGlot reads the
        warehouse's type. Otherwise the columns stay unknown."""
        contract = self._contract_columns(relation)
        if contract is not None:
            return contract
        columns = self.catalog_columns.get(relation.unique_id)
        if columns is None:
            return None
        definitions = []
        for column in columns:
            name = _identifier(column.name, self.dialect)
            try:
                kind = exp.DataType.build(column.data_type or "", dialect=self.dialect) if column.data_type else None
            except (SqlglotError, ValueError):
                kind = None  # Untyped: nothing is assumed of its comparisons.
            definitions.append(f"{name} {kind.sql(dialect=self.dialect)}" if kind is not None else name)
        return definitions

    def _contract_columns(self, relation: Relation) -> list[str] | None:
        if not relation.contract_enforced or not relation.columns:
            return None
        if any(column.data_type is None for column in relation.columns):
            return None
        definitions = []
        for column in relation.columns:
            try:
                kind = exp.DataType.build(column.data_type or "", dialect=self.dialect)
            except (SqlglotError, ValueError):
                return None
            definitions.append(f"{_identifier(column.name, self.dialect)} {kind.sql(dialect=self.dialect)}")
        return definitions

    def _declarations(
        self, program: ir.Program, knowledge: tuple[Property, ...], model: Relation, reached: list[Relation],
    ) -> list[Property]:
        """The fact tests and constraints of every reached relation, as Properties of its IR."""
        named = {
            relation.name.lower(): relation for relation in program.named_relations
            if relation.role in {ir.RelationRole.TABLE, ir.RelationRole.VIEW}
        }
        ctes: dict[str, list[ir.NamedRelation]] = {}
        for relation in program.named_relations:
            if relation.role is ir.RelationRole.CTE:
                ctes.setdefault(relation.name.lower(), []).append(relation)

        def relations_of(node: str) -> list[ir.NamedRelation]:
            relation = self.relations[node]
            if relation.materialized == "ephemeral":
                names = {
                    match.group(1).lower()
                    for source in (model, *reached) for cte in source.extra_ctes
                    if cte.id == node and (match := _CTE_NAME.match(cte.sql))
                }
                return [r for name in names for r in ctes.get(name, [])]
            canonical = _canonical(relation.relation_name, self.dialect)
            return [named[canonical]] if canonical in named else []

        declared: list[Property] = []
        for relation in reached:
            for expectation in self.facts(relation.unique_id):
                for target in relations_of(relation.unique_id):
                    columns = _columns(target, expectation.columns)
                    if columns is None:
                        continue  # A column nothing reads; its facts cannot matter.
                    match expectation.declared:
                        case "unique":
                            declared.append(UniqueSet(columns=frozenset(columns)))
                        case "key":
                            declared.append(NonNullUniqueSet(columns=frozenset(columns)))
                            declared.extend(NonNullColumn(column=column) for column in columns)
                        case "not_null":
                            declared.extend(NonNullColumn(column=column) for column in columns)
                        case "enum":
                            with Engine().session(program, knowledge):
                                identical = _holds_its_literals(columns[0], expectation.values, self.dialect)
                            if identical:
                                declared.append(EnumeratedDomain(column=columns[0], values=frozenset(expectation.values)))
                        case "coverage":
                            referenced = [
                                found for other in relations_of(expectation.referenced or "")
                                if (found := _columns(other, expectation.referenced_columns)) is not None
                            ] if expectation.referenced in self.relations else []
                            declared.extend(
                                ReferentialCoverage(columns=columns, referenced_columns=found) for found in referenced
                            )
                        case None:
                            pass
        return declared

    def _additions(
        self, program: ir.Program, knowledge: tuple[Property, ...], diagnostics: tuple[Diagnostic, ...], model: Relation,
    ) -> tuple[Addition, ...]:
        """Unique Sets, Not Null columns and Enumerated Domains proved of the model."""
        root = program.root
        if root is None or any(d.code in _UNPARSEABLE | {diag.DUPLICATE_DECLARATION} for d in diagnostics):
            return ()
        names = [column.name.lower() for column in root.output_columns]
        columns = tuple(column for column in root.output_columns if names.count(column.name.lower()) == 1)
        found: list[Addition] = []
        with Engine().session(program, knowledge) as arena:
            non_null = {
                column: arena.prove(NonNullColumns(columns=frozenset((column,))))
                for column in columns
            }
            for key in minimal_keys(root):
                if not set(key) <= set(columns):
                    continue
                evidence = arena.prove(UniqueSet(columns=frozenset(key)))
                if not evidence.value:
                    continue
                strict = all(non_null[column].value for column in key)
                found.append(Addition("key" if strict else "unique", tuple(c.name for c in key), (), evidence.explain()))
            for column, evidence in non_null.items():
                if evidence.value:
                    found.append(Addition("not_null", (column.name,), (), evidence.explain()))
            for column in columns:
                if isinstance(column.scalar_expr, ir.Constant):
                    continue
                domain = _domain(column).value
                if domain and len(domain) <= MAX_ENUM_VALUES and len({value.type for value in domain}) == 1:
                    evidence = arena.prove(EnumeratedDomain(column=column, values=domain))
                    if evidence.value:
                        values = tuple(sorted(domain, key=lambda value: value.text))
                        found.append(Addition("enum", (column.name,), values, evidence.explain()))
        return tuple(found)


def _blank_assertions(sql: str) -> str:
    """`sql` with its Assertion Markers blanked: another model's assertions are
    checked when that model is analyzed, not wherever it is read."""
    for match in reversed(_marker_shapes(sql)):
        if re.sub(r"\s+", " ", match.group(0)).upper().startswith("/**ASSERT "):
            sql = sql[:match.start()] + re.sub(r"[^\n]", " ", match.group(0)) + sql[match.end():]
    return sql


def _identifier(name: str, dialect: str) -> str:
    return exp.to_identifier(name).sql(dialect=dialect)


def _canonical(relation_name: str | None, dialect: str) -> str:
    """How the IR names a relation written as `relation_name`."""
    if relation_name is None:
        return ""
    try:
        table = exp.to_table(relation_name, dialect=dialect)
    except SqlglotError:
        return relation_name.lower()
    return ".".join(part for part in (table.catalog, table.db, table.name) if part).lower()


def _columns(relation: ir.NamedRelation, names: tuple[str, ...]) -> tuple[ir.OutputColumn, ...] | None:
    found = []
    for name in names:
        matching = [column for column in relation.output_columns if column.name.lower() == name.lower()]
        if len(matching) != 1:
            return None
        found.append(matching[0])
    return tuple(found)


def _coverage(assertion: ir.Assertion) -> ir.Assertion:
    """A relationships test checks Referential Coverage only, never the
    referenced Unique Set a Foreign Key Assertion also requires."""
    property = assertion.property
    assert isinstance(property, ForeignKey)
    coverage = ReferentialCoverage(columns=property.columns, referenced_columns=property.referenced_columns)
    return ir.Assertion(origin=assertion.origin, property=coverage)


def _tests(report: Report, tested: Mapping[int, Expectation]) -> dict[str, TestResult]:
    results: dict[str, TestResult] = {}
    for assertion in report.assertions:
        expectation = tested.get(assertion.origin.line or 0)
        if expectation is not None:
            explanation = assertion.evidence.explain() if assertion.evidence is not None else ""
            results[expectation.unique_id] = TestResult(assertion.outcome is Outcome.PROVED, explanation)
    for diagnostic in (*report.diagnostics, *report.warnings):
        line = diagnostic.origin.line if diagnostic.origin is not None else None
        expectation = tested.get(line or 0)
        if expectation is not None and expectation.unique_id not in results:
            results[expectation.unique_id] = TestResult(False, f"{diagnostic.code}: {diagnostic.message}")
    for line, expectation in tested.items():
        results.setdefault(expectation.unique_id, TestResult(False, "it was not analyzed"))
    return results


def _holds_its_literals(column: ir.OutputColumn, values: Iterable[LiteralValue], dialect: str) -> bool:
    """Whether `column IN (values)` passing means each value of `column` is one
    of `values` as written: the comparison casts neither side and equates no
    two distinct values. An integer column passes `IN ('01')` with 1, a
    decimal one `IN (1)` with 1.00; of an unknown type nothing is known."""
    declared = _comparison_type(column).value
    if declared is None or not _identity_comparison(column).value:
        return False
    if _binary_text(column).value:
        return all(value.type == "string" for value in values)
    kind = exp.DataType.build(declared, dialect=dialect).this
    if kind in exp.DataType.INTEGER_TYPES:
        return all(value.type == "numeric" and value.text.lstrip("-").isdigit() for value in values)
    if kind == exp.DataType.Type.BOOLEAN:
        return all(value.type == "boolean" for value in values)
    return False


def _markers(report: Report, body: Body, body_lines: int) -> tuple[MarkerResult, ...]:
    found = []
    for assertion in report.assertions:
        span = assertion.origin.span
        if span is None or span.line > body_lines:
            continue
        start = _line_start(body, span.line)
        offset = start + span.column - 1
        if body.in_injected(offset):
            continue
        end = body.text.find("\n", start) % (len(body.text) + 1)  # -1: the last line runs to the end.
        on_line = [m.start() for m in _marker_shapes(body.text) if start <= m.start() < end]
        found.append(MarkerResult(
            span.line, raw_line_of(body, span.line, offset), on_line.index(offset) if offset in on_line else 0,
            _marker_at(body, offset), assertion.outcome,
            assertion.evidence.explain() if assertion.evidence is not None else "",
        ))
    return tuple(found)


def _problems(
    diagnostics: Iterable[Diagnostic], warnings: Iterable[Diagnostic], body: Body, body_lines: int,
    tested: Mapping[int, Expectation], reached: list[Relation], dialect: str,
) -> tuple[Problem, ...]:
    """The Diagnostics about the model's own SQL; those about another relation
    are reported when that relation is analyzed, and a table with unknown
    columns is reported once for the project (the model's own, read through
    `{{ this }}`, is its previous run and unknown by nature)."""
    reached_names = {_canonical(relation.relation_name, dialect) for relation in reached}
    found = []
    for diagnostic, warning in [*((d, False) for d in diagnostics), *((d, True) for d in warnings)]:
        line = diagnostic.origin.line if diagnostic.origin is not None else None
        if diagnostic.code == diag.UNDECLARED_TABLE and any(
            f"table {name} has" in diagnostic.message.lower() for name in reached_names
        ):
            continue
        if line is not None and (line > body_lines or line in tested or body.in_injected(_line_offset(body, line))):
            continue
        found.append(Problem(diagnostic, warning, line, raw_line_of(body, line) if line is not None else None))
    return tuple(found)


def _site_line(body: Body, line: int, anchor: int, trailing: bool) -> int | None:
    """The model file's line of a suggestion's site, when known. Without a
    trusted rendering, a line is found by its whole text (a trailing site's own
    text is a mere ")" or alike), else by the lines around it: when every line
    before it, or every line after it, is the model file's, it is that line."""
    if body.lines is not None or not trailing:
        found = raw_line_of(body, line, anchor)
    else:
        line = body.text.count("\n", 0, anchor) + 1
        found = raw_line_of(body, line)
    if found is not None or body.lines is not None:
        return found
    compiled, raw = body.text.split("\n"), _strip_end(body.raw_code).split("\n")
    if compiled[:line - 1] == raw[:line - 1] and line <= len(raw):
        return line
    after = len(compiled) - line
    if after and compiled[line:] == raw[len(raw) - after:] and len(raw) > after:
        return len(raw) - after
    return None


def _line_start(body: Body, line: int) -> int:
    """The offset where `line` of the body starts."""
    start = 0
    for _ in range(line - 1):
        start = body.text.find("\n", start) + 1
        if start == 0:
            return len(body.text)
    return start


def _line_offset(body: Body, line: int) -> int:
    """The offset where `line` of the body starts its first marker, or its first non-space."""
    start = _line_start(body, line)
    if start == len(body.text):
        return start
    end = body.text.find("\n", start)
    end = len(body.text) if end < 0 else end
    markers = [m for m in _marker_shapes(body.text) if start <= m.start() < end]
    if markers:
        return markers[0].start()
    stripped = len(body.text[start:end]) - len(body.text[start:end].lstrip())
    return start + stripped


def _marker_at(body: Body, offset: int) -> str:
    match = next((m for m in _marker_shapes(body.text) if m.start() == offset), None)
    return re.sub(r"\s+", " ", match.group(0)) if match else ""


def raw_line_of(body: Body, line: int, offset: int | None = None) -> int | None:
    """The model file's line that `line` of the body was rendered from, when known.

    Without a trusted rendering, a marker is found by its text: the k-th
    occurrence in the body is the k-th in the raw code, when both have equally
    many; any other line, by its text when that occurs once in each.
    `offset` is the marker's, when `line` has more than one."""
    offset = _line_offset(body, line) if offset is None else offset
    if body.lines is not None:
        found = body.lines[offset] if offset < len(body.lines) else 0
        return found or None
    marker = next((m for m in _marker_shapes(body.text) if m.start() == offset), None)
    if marker is not None:
        text = marker.group(0)
        compiled = [m.start() for m in _marker_shapes(body.text) if m.group(0) == text and not body.in_injected(m.start())]
        raw = [m.start() for m in _marker_shapes(body.raw_code) if m.group(0) == text]
        if len(compiled) != len(raw) or offset not in compiled:
            return None
        return body.raw_code.count("\n", 0, raw[compiled.index(offset)]) + 1
    end = body.text.find("\n", offset)
    text = body.text[offset:len(body.text) if end < 0 else end].strip()
    if not text or body.text.count(text) != 1 or body.raw_code.count(text) != 1:
        return None
    return body.raw_code.count("\n", 0, body.raw_code.index(text)) + 1


__all__ = ["Body", "Branches", "Builder", "BranchResult", "SuggestResult", "Suggested", "branches", "raw_line_of"]
