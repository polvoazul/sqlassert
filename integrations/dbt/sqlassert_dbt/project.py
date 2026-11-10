"""Analyze a dbt project from its `target/manifest.json`, without dbt.

Each model is analyzed in a Program of its own (`program.py`): its own tests
are Assertions, the passing tests of what it reads are Property Declarations.
An incremental model is analyzed once per `is_incremental()` branch, and a
claim about it holds only when it holds in both.

Opted into, `catalog.json` lists the columns of the tables a model reads
(sources, seeds, snapshots, incremental models) that the manifest leaves
unknown. It describes the warehouse when `dbt docs generate` ran, so it is
trusted like a connection: it may add proofs, never change what the SQL and
the manifest alone prove. An entry is left out where the project shows it is
stale: another target's relation, a seed whose CSV has other columns, a model
whose enforced contract lists others.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from sqlassert_dbt.expectations import Expectation, PropertyKind, expectations
from sqlassert_dbt.manifest import Catalog, CatalogTable, Column, Manifest, ManifestError, Relation, load_catalog, load_manifest
from sqlassert_dbt.program import (
    QUERY_MATERIALIZATIONS, Body, BranchResult, Builder, Suggested, SuggestResult, _canonical, branches, raw_line_of,
)
from sqlassert_dbt.report import (
    DbtReport, Location, MarkerOutcome, ModelSuggestion, Problem, Status, SuggestReport, TestOutcome, Untested,
)
from sqlassert.dialects import MODELLED_DIALECTS
from sqlassert.improvements import Level
from sqlassert.ir.model import LiteralValue
from sqlassert.reporting import Outcome
from sqlassert.sql_parse import _marker_shapes

# The dialects sqlassert analyzes soundly: those `dialects.py` models. A dbt
# adapter maps onto its SQLGlot dialect; one not modelled is analyzed only
# under a dialect the user names explicitly.
SUPPORTED_DIALECTS = MODELLED_DIALECTS
_ADAPTER_DIALECTS = {
    "duckdb": "duckdb", "postgres": "postgres", "redshift": "redshift", "snowflake": "snowflake", "bigquery": "bigquery",
    "databricks": "databricks", "spark": "spark", "trino": "trino", "mysql": "mysql", "sqlserver": "tsql",
    "fabric": "tsql", "athena": "athena", "clickhouse": "clickhouse", "oracle": "oracle",
}

# Incremental strategies whose table stays unique on `unique_key` when every
# batch is: matching rows are replaced, never appended.
_REPLACING_STRATEGIES = {"merge", "delete+insert"}

UNSUPPORTED_DIALECT = "dbt-unsupported-dialect"
UNANALYZED_MODEL = "dbt-unanalyzed-model"
UNANALYZED_BRANCH = "dbt-unanalyzed-branch"
UNKNOWN_COLUMNS = "dbt-unknown-columns"
EPHEMERAL_NOT_LOCATED = "dbt-ephemeral-not-located"
UNTRUSTED_UPSTREAM_TESTS = "dbt-untrusted-upstream-tests"
CATALOG_OTHER_RELATION = "dbt-catalog-other-relation"
CATALOG_STALE = "dbt-catalog-stale"


class ProjectError(Exception):
    """The project cannot be analyzed at all: no manifest, or an unreadable one (or catalog)."""


def analyze_project(
    project_dir: Path, *, manifest_path: Path | None = None, dialect: str | None = None, trust_upstream_tests: bool = False,
    catalog_path: Path | None = None,
) -> DbtReport:
    """Every test and marker of the dbt project at `project_dir`.

    `dialect` overrides the one the manifest's adapter names; without it, an
    adapter sqlassert does not support leaves every model unanalyzed.
    `trust_upstream_tests` makes the passing tests of what a model reads facts
    about it, which holds when they run before it, as under `dbt build`.
    `catalog_path` names a `catalog.json` whose columns and types complete
    the tables the manifest leaves unknown.
    """
    return _analysis(project_dir, manifest_path, trust_upstream_tests, catalog_path).run(dialect)


def suggest_models(
    project_dir: Path, files: list[str], *, manifest_path: Path | None = None, dialect: str | None = None,
    trust_upstream_tests: bool = False, catalog_path: Path | None = None,
) -> SuggestReport:
    """What `sqlassert suggest` finds in each model whose file is one of
    `files` (paths relative to `project_dir`), with the relations each reads
    declared as `analyze_project` declares them."""
    analysis = _analysis(project_dir, manifest_path, trust_upstream_tests, catalog_path)
    manifest = analysis.manifest
    by_path = {
        relation.original_file_path: relation for relation in manifest.relations.values() if relation.resource_type == "model"
    }
    models = []
    for file in files:
        relation = by_path.get(Path(file).as_posix())
        if relation is None:
            raise ProjectError(f"{file} is not a model of the project in {manifest_path or project_dir / 'target' / 'manifest.json'}")
        models.append(relation)
    return analysis.suggest(dialect, models)


def _analysis(project_dir: Path, manifest_path: Path | None, trust_upstream_tests: bool, catalog_path: Path | None) -> _Analysis:
    path = manifest_path or project_dir / "target" / "manifest.json"
    try:
        manifest = load_manifest(path)
    except ManifestError as error:
        raise ProjectError(f"{error}; run `dbt compile` (or `dbt build`) first") from error
    try:
        catalog = load_catalog(catalog_path) if catalog_path is not None else None
    except ManifestError as error:
        raise ProjectError(f"{error}; `dbt docs generate` writes target/catalog.json") from error
    return _Analysis(manifest, tuple(expectations(manifest)), trust_upstream_tests, catalog, project_dir)


@dataclass
class _Analysis:
    manifest: Manifest
    expectations: tuple[Expectation, ...]
    trust_upstream_tests: bool = False
    catalog: Catalog | None = None
    project_dir: Path = Path(".")

    def _builder(self, dialect: str, problems: list[Problem]) -> Builder:
        columns, found = _catalog_columns(self.manifest, self.catalog, self.project_dir, dialect)
        problems.extend(found)
        return Builder(self.manifest.relations, self.expectations, dialect, self.trust_upstream_tests, catalog_columns=columns)

    def run(self, requested: str | None) -> DbtReport:
        problems: list[Problem] = []
        dialect = self._dialect(requested, problems)
        tests: list[TestOutcome] = []
        markers: list[MarkerOutcome] = []
        untested: list[Untested] = []
        undeclared: set[str] = set()
        builder = self._builder(dialect, problems) if dialect is not None else None
        for relation in sorted(self.manifest.relations.values(), key=lambda relation: relation.unique_id):
            own = [e for e in self.expectations if e.node == relation.unique_id]
            if builder is None:
                tests.extend(self._outcome(e, relation, Status.NOT_PROVED, "the project's dialect is not supported") for e in own)
                continue
            outcome = self._model(builder, relation, own)
            tests.extend(outcome.tests)
            markers.extend(outcome.markers)
            untested.extend(outcome.untested)
            problems.extend(outcome.problems)
            undeclared |= outcome.undeclared
        if builder is not None:
            problems.extend(self._shared_problems(builder, undeclared))
        return DbtReport(dialect, tuple(tests), tuple(markers), tuple(untested), tuple(_unique(problems)))

    def suggest(self, requested: str | None, models: list[Relation]) -> SuggestReport:
        problems: list[Problem] = []
        dialect = self._dialect(requested, problems)
        if dialect is None:
            return SuggestReport(None, (), (), tuple(problems))
        builder = self._builder(dialect, problems)
        found: list[ModelSuggestion] = []
        undeclared: set[str] = set()
        for relation in models:
            unanalyzed = self._unanalyzed(relation)
            if unanalyzed is not None:
                problems.append(unanalyzed)
                continue
            bodies = branches(relation, self.manifest.relations)
            for warning in bodies.warnings:
                problems.append(Problem(EPHEMERAL_NOT_LOCATED, warning, "warning", Location(relation.original_file_path), relation.name))
            if bodies.unrendered is not None:
                problems.append(Problem(
                    UNANALYZED_BRANCH, f"model {relation.name}: {bodies.unrendered}; only its smells are listed",
                    "warning", Location(relation.original_file_path), relation.name,
                ))
            own = [e for e in self.expectations if e.node == relation.unique_id]
            results = [builder.suggest(relation, body, own) for body in bodies.bodies]
            for body, result in zip(bodies.bodies, results):
                undeclared |= result.undeclared
                problems.extend(self._problems(relation, [(body, result)]))
            found.extend(_combined(relation, results, complete=bodies.unrendered is None))
        problems.extend(self._shared_problems(builder, undeclared))
        analyzed = tuple(relation.name for relation in models if self._unanalyzed(relation) is None)
        return SuggestReport(dialect, analyzed, tuple(found), tuple(_unique(problems)))

    def _shared_problems(self, builder: Builder, undeclared: set[str]) -> list[Problem]:
        """The warnings about the relations models read, once for the project."""
        problems = []
        if builder.untrusted:
            problems.append(Problem(
                UNTRUSTED_UPSTREAM_TESTS,
                f"{len(builder.untrusted)} passing test(s) of the relations models read were not used as facts, since nothing "
                "says they ran before those models were built: pass --trust-upstream-tests when they do (dbt build), "
                "or run through `sqlassert-dbt build`",
                "warning",
            ))
        for node in sorted(undeclared):
            relation = self.manifest.relations[node]
            catalog = (
                "catalog.json lists none it could use (see any dbt-catalog warning)" if self.catalog is not None
                else "--catalog target/catalog.json, after `dbt docs generate`, lists them"
            )
            supply = (
                f"an enforced model contract listing every column's data_type declares them, or {catalog}"
                if relation.resource_type == "model" else catalog
            )
            problems.append(Problem(
                UNKNOWN_COLUMNS,
                f"the columns of {relation.resource_type} {relation.label} are not all known from the manifest, so its keys "
                f"and nullability come from its passing tests only: {supply}",
                "warning", Location(relation.patch_path or relation.original_file_path), relation.label,
            ))
        return problems

    def _unanalyzed(self, relation: Relation) -> Problem | None:
        """Why a model's SQL cannot be analyzed, or None."""
        if relation.language == "sql" and relation.compiled_code is not None:
            return None
        why = f"it is a {relation.language} model" if relation.language != "sql" else "it has no compiled code; run `dbt compile`"
        # Its own markers go unchecked: that fails the check, not a fact a connection could supply.
        severity = "error" if relation.language == "sql" and _has_markers(relation.raw_code) else "warning"
        return Problem(
            UNANALYZED_MODEL, f"model {relation.name} was not analyzed: {why}", severity,
            Location(relation.original_file_path), relation.name,
        )

    def _dialect(self, requested: str | None, problems: list[Problem]) -> str | None:
        if requested is not None:
            return requested
        adapter = self.manifest.adapter_type
        dialect = _ADAPTER_DIALECTS.get(adapter or "")
        if dialect in SUPPORTED_DIALECTS:
            return dialect
        problems.append(Problem(
            UNSUPPORTED_DIALECT,
            f"the {adapter or 'unknown'} adapter's SQL dialect is not supported (supported: {', '.join(sorted(SUPPORTED_DIALECTS))}), "
            "so no model was analyzed; pass --dialect to analyze it under a dialect whose semantics you vouch for",
            "error",
        ))
        return None

    def _model(self, builder: Builder, relation: Relation, own: list[Expectation]) -> _ModelOutcome:
        outcome = _ModelOutcome()
        if relation.resource_type != "model":
            outcome.tests.extend(
                self._outcome(e, relation, Status.NOT_PROVED, f"a {relation.resource_type} is a table: only its own data shows this")
                for e in own
            )
            return outcome
        unanalyzed = self._unanalyzed(relation)
        if unanalyzed is not None:
            outcome.problems.append(unanalyzed)
            outcome.tests.extend(self._outcome(e, relation, Status.NOT_PROVED, "its model was not analyzed") for e in own)
            return outcome

        found = branches(relation, self.manifest.relations)
        for warning in found.warnings:
            outcome.problems.append(Problem(EPHEMERAL_NOT_LOCATED, warning, "warning", Location(relation.original_file_path), relation.name))
        if found.unrendered is not None:
            outcome.problems.append(Problem(
                UNANALYZED_BRANCH, f"model {relation.name}: {found.unrendered}", "warning", Location(relation.original_file_path), relation.name,
            ))
        analyzed = tuple(e for e in own if e.asserted is not None and e.unprovable is None)
        results = [(body, builder.analyze(relation, body, analyzed)) for body in found.bodies]
        complete = found.unrendered is None
        for _, result in results:
            outcome.undeclared |= result.undeclared

        for expectation in own:
            outcome.tests.append(self._test(expectation, relation, results, complete, found.unrendered))
        outcome.markers.extend(self._markers(relation, results, found.unrendered))
        outcome.problems.extend(self._problems(relation, results))
        if complete and relation.materialized != "ephemeral":
            outcome.untested.extend(self._untested(relation, own, results))
        return outcome

    def _outcome(self, e: Expectation, relation: Relation, status: Status, reason: str, explanation: str = "") -> TestOutcome:
        return TestOutcome(e.unique_id, e.name, e.kind, relation.label, Location(e.path), status, reason, explanation)

    def _test(
        self, e: Expectation, relation: Relation, results: list[tuple[Body, BranchResult]], complete: bool, unrendered: str | None,
    ) -> TestOutcome:
        if e.asserted is None or e.unprovable is not None:
            return self._outcome(e, relation, Status.NOT_PROVED, e.unprovable or "it states no Property")
        found = [test for _, result in results if (test := result.tests.get(e.unique_id)) is not None]
        failed = next((test for test in found if not test.proved), None)
        if failed is not None or len(found) != len(results):
            reason = "its Property is not proved from the SQL"
            return self._outcome(e, relation, Status.NOT_PROVED, reason, failed.explanation if failed else "")
        explanation = found[0].explanation
        if not complete:
            return self._outcome(e, relation, Status.NOT_PROVED, unrendered or "a branch was not analyzed", explanation)
        reason = self._materialization_gap(e, relation)
        if reason is not None:
            return self._outcome(e, relation, Status.NOT_PROVED, reason, explanation)
        return self._outcome(e, relation, Status.PROVED, "", explanation)

    def _materialization_gap(self, e: Expectation, relation: Relation) -> str | None:
        """Why a Property of the model's query is not one of its relation, or None."""
        if relation.materialized in QUERY_MATERIALIZATIONS | {"ephemeral"}:
            return None
        if relation.materialized != "incremental":
            return f"a {relation.materialized} materialization's rows are not known to be its query's"
        match e.asserted:
            case "not_null" | "enum":
                return None  # Every row of the table came from one batch.
            case "unique" | "key":
                if relation.incremental_predicates:
                    return (
                        "its incremental_predicates can keep an earlier row out of the match on its unique_key, "
                        "so a key of every batch is not one of the table"
                    )
                key = {column.lower() for column in relation.unique_key}
                if relation.incremental_strategy in _REPLACING_STRATEGIES and key == {column.lower() for column in e.columns}:
                    return None
                return (
                    "an incremental table keeps rows of earlier runs, so a key of every batch is a key of the table only "
                    "under a merge or delete+insert strategy on that unique_key"
                )
            case _:
                return "an incremental table keeps rows of earlier runs, whose references may since have gone"

    def _markers(self, relation: Relation, results: list[tuple[Body, BranchResult]], unrendered: str | None) -> list[MarkerOutcome]:
        grouped: dict[tuple, list] = {}
        for index, (_, result) in enumerate(results):
            for marker in result.markers:
                key = (
                    (marker.raw_line, marker.place, marker.marker) if marker.raw_line is not None
                    else (None, index, marker.line, marker.place, marker.marker)
                )
                grouped.setdefault(key, []).append(marker)
        found = []
        for markers in grouped.values():
            first = markers[0]
            failed = next((marker for marker in markers if marker.outcome is Outcome.UNKNOWN), None)
            outcome, explanation = (Outcome.UNKNOWN, failed.explanation) if failed else (Outcome.PROVED, first.explanation)
            if outcome is Outcome.PROVED and unrendered is not None:
                outcome, explanation = Outcome.UNKNOWN, f"{explanation}\nnot proved for every run: {unrendered}"
            location = (
                Location(relation.original_file_path, first.raw_line) if first.raw_line is not None
                else Location(relation.compiled_path or relation.original_file_path, first.line)
            )
            found.append(MarkerOutcome(relation.name, location, first.marker, outcome, explanation))
        return found

    def _problems(self, relation: Relation, results: list[tuple[Body, BranchResult]] | list[tuple[Body, SuggestResult]]) -> list[Problem]:
        found = []
        for body, result in results:
            for problem in result.problems:
                diagnostic = problem.diagnostic
                if problem.raw_line is not None:
                    location = Location(relation.original_file_path, problem.raw_line)
                elif problem.line is not None:
                    location = Location(relation.compiled_path or relation.original_file_path, problem.line)
                else:
                    location = Location(relation.original_file_path)
                message = _raw_lines_in(diagnostic.message, body) if problem.raw_line is not None else diagnostic.message
                found.append(Problem(diagnostic.code, message, "warning" if problem.warning else "error", location, relation.name))
        return found

    def _untested(self, relation: Relation, own: list[Expectation], results: list[tuple[Body, BranchResult]]) -> list[Untested]:
        incremental = relation.materialized == "incremental"
        if relation.materialized not in QUERY_MATERIALIZATIONS and not incremental:
            return []
        additions = [
            {(a.kind, a.columns, a.values): a for a in result.additions
             if not incremental or a.kind in {"not_null", "enum"}}
            for _, result in results
        ]
        common = [addition for key, addition in additions[0].items() if all(key in other for other in additions[1:])]
        stated = [(e.asserted, {c.lower() for c in e.columns}) for e in own]
        location = Location(relation.patch_path or relation.original_file_path)
        found = []
        for addition in common:
            columns = {column.lower() for column in addition.columns}
            entry = _schema_entry(addition.kind, addition.columns, addition.values, stated, columns)
            if entry is not None:
                column = addition.columns[0] if len(addition.columns) == 1 else None
                found.append(Untested(relation.name, location, column, entry, addition.explanation))
        return found


@dataclass
class _ModelOutcome:
    tests: list[TestOutcome]
    markers: list[MarkerOutcome]
    untested: list[Untested]
    problems: list[Problem]
    undeclared: set[str]

    def __init__(self) -> None:
        self.tests, self.markers, self.untested, self.problems, self.undeclared = [], [], [], [], set()


def _catalog_columns(
    manifest: Manifest, catalog: Catalog | None, project_dir: Path, dialect: str,
) -> tuple[dict[str, tuple[Column, ...]], list[Problem]]:
    """The catalog's columns of each relation, unless the project shows the
    entry stale, and a warning for each one left out."""
    if catalog is None:
        return {}, []
    columns: dict[str, tuple[Column, ...]] = {}
    problems: list[Problem] = []
    other: list[str] = []
    for unique_id, table in catalog.tables.items():
        relation = manifest.relations.get(unique_id)
        if relation is None:
            continue
        if _canonical(relation.relation_name, dialect) != _name_of(table).lower():
            other.append(f"{relation.resource_type} {relation.label} is {_name_of(table)} there, {relation.relation_name} here")
            continue
        listed = [column.name.lower() for column in table.columns]
        where = Location(relation.patch_path or relation.original_file_path)
        if relation.resource_type == "seed":
            header = _csv_header(project_dir / relation.original_file_path)
            if header is not None and [name.lower() for name in header] != listed:
                problems.append(Problem(
                    CATALOG_STALE,
                    f"catalog.json lists columns {', '.join(listed)} for seed {relation.name}, but its CSV has "
                    f"{', '.join(header)}: it predates the seed's last change, so they were not used; "
                    "run `dbt seed` and `dbt docs generate`",
                    "warning", Location(relation.original_file_path), relation.label,
                ))
                continue
        contract = [column.name.lower() for column in relation.columns]
        if relation.contract_enforced and contract and sorted(contract) != sorted(listed):
            problems.append(Problem(
                CATALOG_STALE,
                f"catalog.json lists columns {', '.join(listed)} for model {relation.name}, but its enforced contract "
                f"lists {', '.join(contract)}: the contract is used; run `dbt build` and `dbt docs generate`",
                "warning", where, relation.label,
            ))
        columns[unique_id] = table.columns
    if other:
        problems.append(Problem(
            CATALOG_OTHER_RELATION,
            f"catalog.json names {len(other)} relation(s) differently from the manifest, so their columns were not used: "
            f"it was generated for another target ({other[0]}); run `dbt docs generate` with this target",
            "warning",
        ))
    return columns, problems


def _name_of(table: CatalogTable) -> str:
    return ".".join(part for part in (table.database, table.schema, table.name) if part)


def _csv_header(path: Path) -> list[str] | None:
    """A seed's column names, None when its file cannot be read."""
    try:
        with path.open(newline="") as file:
            return next(csv.reader(file), None)
    except (OSError, csv.Error, UnicodeDecodeError):
        return None


def _combined(relation: Relation, results: list[SuggestResult], *, complete: bool) -> list[ModelSuggestion]:
    """What every branch suggests: a rewrite or marker must hold in each run
    dbt may do, so it is kept only when every branch was analyzed and proves
    it; a smell in any branch is one. The Root Select may end on another line
    in each branch, so a marker after it is matched by its text alone, and
    placed after the latest end."""
    order = {Level.HIGH: 0, Level.MID: 1, Level.LOW: 2}
    keyed: list[dict[tuple, list[Suggested]]] = []
    for index, result in enumerate(results):
        found: dict[tuple, list[Suggested]] = {}
        for item in result.suggested:
            if item.trailing and item.label == "Root Select":
                where: tuple = ("end",)
            else:
                where = (item.raw_line,) if item.raw_line is not None else (None, index, item.line)
            found.setdefault(where + (item.label, item.kind, item.text), []).append(item)
        keyed.append(found)
    kept: dict[tuple, list[Suggested]] = {}
    for found in keyed:
        for key, items in found.items():
            proved = items[0].level is not Level.LOW
            if proved and not (complete and all(key in other for other in keyed)):
                continue
            kept.setdefault(key, []).extend(items)
    placed = []
    for key, items in kept.items():
        item = items[0]
        if key[0] == "end":
            lines = [found.raw_line for found in items if found.raw_line is not None]
            raw_line = _past_block_ends(relation.raw_code, max(lines)) if len(lines) == len(items) else None
        else:
            raw_line = item.raw_line
        placed.append((item, raw_line))
    placed.sort(key=lambda pair: (order[pair[0].level], pair[1] or pair[0].line, pair[0].line))
    return [
        ModelSuggestion(
            relation.name,
            Location(relation.original_file_path, raw_line) if raw_line is not None
            else Location(relation.compiled_path or relation.original_file_path, item.line),
            item.level.value, item.label, item.kind, item.text, item.explanation,
        )
        for item, raw_line in placed
    ]


_BLOCK_END = re.compile(r"\s*\{%-?\s*end\w*\s*-?%\}\s*")


def _past_block_ends(raw_code: str, line: int) -> int:
    """`line`, or the last of the lines right after it that only close a Jinja
    block: a marker pasted at its end is outside every block `line` is in."""
    lines = raw_code.split("\n")
    while line < len(lines) and _BLOCK_END.fullmatch(lines[line]):
        line += 1
    return line


def _schema_entry(
    kind: PropertyKind, columns: tuple[str, ...], values: tuple[LiteralValue, ...],
    stated: list[tuple[PropertyKind | None, set[str]]], members: set[str],
) -> str | None:
    """The schema.yml test stating a proved Property, None when a test already
    states it or no standard test does (a nullable composite Unique Set)."""
    def tested(*kinds: str) -> bool:
        return any(asserted in kinds and found == members for asserted, found in stated)

    match kind:
        case "not_null":
            return None if tested("not_null") or any(a == "key" and members <= found for a, found in stated) else "not_null"
        case "enum":
            if tested("enum"):
                return None
            listed = ", ".join(value.sql() if value.type == "string" else value.text for value in values)
            quote = "" if all(value.type == "string" for value in values) else ", quote: false"
            return f"accepted_values: {{values: [{listed}]{quote}}}"
        case "unique" | "key" if len(columns) == 1:
            return None if tested("unique", "key") else "unique"
        case "key":
            if tested("unique", "key"):
                return None
            return f"dbt_utils.unique_combination_of_columns: {{combination_of_columns: [{', '.join(columns)}]}}"
        case _:
            return None


def _raw_lines_in(message: str, body: Body) -> str:
    """`message` with each body line it names replaced by the model file's line."""
    def replaced(match: re.Match[str]) -> str:
        line = raw_line_of(body, int(match.group(1)))
        return f"line {line}" if line is not None else match.group(0)

    return re.sub(r"\bline (\d+)\b", replaced, message)


def _unique(problems: list[Problem]) -> list[Problem]:
    return list(dict.fromkeys(problems))


_PROPERTY_MARKER = re.compile(r"/\*\*\s*(ASSERT|DECLARE)\b", re.IGNORECASE)


def _has_markers(sql: str) -> bool:
    """Whether `sql` writes a Property Marker."""
    return any(_PROPERTY_MARKER.match(match.group()) for match in _marker_shapes(sql))
