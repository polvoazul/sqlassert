"""Convert a parsed SQL Program into the relational IR and the Knowledge
its Create Statements declare.

This is the only module that reads SQLGlot nodes and writes IR values, so it is
the seam where syntax stops and semantics begin. Conversion is two-pass:
declarations are registered first, then bodies are lowered, so that a definition
may reference one declared later.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Iterator

from sqlglot import exp

from sqlassert import diagnostics as diag
from sqlassert import ir, naming
from sqlassert.diagnostics import Diagnostic
from sqlassert.knowledge import ColumnKnowledge, Knowledge, RelationKnowledge, UniqueSetKnowledge
from sqlassert.naming import NameGiver
from sqlassert.sql_parse import ParsedProgram, assertion_line, join_origin
from sqlassert.provenance import SQL, Origin, OriginRegistry


@dataclass(frozen=True)
class IrConversionResult:
    """What one conversion produced: the IR, the Knowledge it declared, and
    everything about the program it could not model."""

    program: ir.Program
    knowledge: Knowledge
    diagnostics: tuple[Diagnostic, ...] = ()

    def merged_with(self, knowledge: Knowledge | None) -> "IrConversionResult":
        """The same conversion, with caller-supplied Knowledge folded in."""
        return replace(self, knowledge=self.knowledge.merge(knowledge))


@dataclass
class CteScope:
    """The CTE names visible while lowering one query.

    A CTE name must never resolve to a declared relation of the same name,
    whether or not its own body is one this slice can lower -- so `declare`
    always shadows the name, and only separately remembers a lowerable body.
    `resolving` guards a body that (directly or through another CTE) refers
    back to itself: valid only under `RECURSIVE`, which this slice does not
    model, so a self-reference must stop the recursion rather than loop.
    """

    _shadowed: set[str] = field(default_factory=set)
    _bodies: dict[str, exp.Select] = field(default_factory=dict)
    _resolving: set[str] = field(default_factory=set)

    def declare(self, name: str, body: exp.Expression) -> None:
        self._shadowed.add(name)
        if isinstance(body, exp.Select):
            self._bodies[name] = body

    def shadows(self, name: str) -> bool:
        return name in self._shadowed

    def body(self, name: str) -> exp.Select | None:
        return None if name in self._resolving else self._bodies.get(name)

    @contextmanager
    def resolving(self, name: str) -> Iterator[None]:
        self._resolving.add(name)
        try:
            yield
        finally:
            self._resolving.discard(name)


@dataclass
class ViewScope:
    """The Create View bodies declared anywhere in the program.

    Unlike a CTE, a view is visible to every scope regardless of declaration
    order, so bodies are recorded once during the declaration pass rather than
    per-query. `resolving` marks a view as on the current lowering path: a
    reference encountered while its own body is still being lowered is a
    cycle, not a valid forward reference, and must be reported rather than
    recursed into.
    """

    _bodies: dict[str, exp.Select] = field(default_factory=dict)
    _resolving: set[str] = field(default_factory=set)

    def declare(self, name: str, body: exp.Expression) -> None:
        if isinstance(body, exp.Select):
            self._bodies[name] = body

    def body(self, name: str) -> exp.Select | None:
        return self._bodies.get(name)

    def is_resolving(self, name: str) -> bool:
        return name in self._resolving

    @contextmanager
    def resolving(self, name: str) -> Iterator[None]:
        self._resolving.add(name)
        try:
            yield
        finally:
            self._resolving.discard(name)


@dataclass
class IrParser:
    """Lowers one parsed SQL Program to the IR.

    It creates the NameGiver and OriginRegistry the analysis runs on, and later
    stages take them from here. It accumulates the declarations, instances, and
    assertions of one program, so an instance analyses exactly one program.
    """

    dialect: str
    names: NameGiver = field(default_factory=NameGiver)
    origins: OriginRegistry = field(default_factory=OriginRegistry)
    _definitions: dict[str, ir.RelationDefinition] = field(default_factory=dict)
    _anonymous: list[ir.RelationDefinition] = field(default_factory=list)
    _declared: list[RelationKnowledge] = field(default_factory=list)
    _ctes: CteScope = field(default_factory=CteScope)
    _views: ViewScope = field(default_factory=ViewScope)
    _assertions: list[ir.UniqueJoinAssertion] = field(default_factory=list)
    _diagnostics: list[Diagnostic] = field(default_factory=list)

    def parse(self, ast: ParsedProgram) -> IrConversionResult:
        for statement in ast.create_statements:
            self._declare(statement) # first pass

        root = self._lower_query(ast.root_select)
        self._report_unanalyzed(ast)

        definitions = tuple(self._definitions.values()) + tuple(self._anonymous)
        program = ir.Program(definitions, root, tuple(self._assertions))
        return IrConversionResult(program, Knowledge(tuple(self._declared)), tuple(self._diagnostics))

    def _report_unanalyzed(self, ast: ParsedProgram) -> None:
        """Report every asserted join this slice never reached.

        An assertion the engine silently ignored would read as a proof, so an
        asserted join in a part of the program that is not yet lowered — a CTE
        body, a subquery, a view definition — is reported instead of dropped.
        """
        analyzed = {self.origins.resolve(assertion.origin_id).line for assertion in self._assertions}

        for join in _asserted_joins(ast):
            line = assertion_line(join)
            if line in analyzed:
                continue
            self._diagnostics.append(
                Diagnostic(
                    diag.UNANALYZED_ASSERTION,
                    f"the unique join assertion on line {line} is in a part of the program this analysis does not model",
                    join_origin(join, self.dialect),
                )
            )

    # Declaration pass ---------------------------------------------------------

    def _declare(self, statement: exp.Create) -> None:
        name = self._extract_table_name(statement)
        if name is None:
            return

        origin = Origin(SQL, statement.sql(dialect=self.dialect))
        if self._reject_duplicate(name, origin):
            return

        self._register_definition(name, origin)
        self._register_table_knowledge(name, statement)
        self._register_view_body(name, statement)

    def _extract_table_name(self, statement: exp.Create) -> str | None:
        table = _created_table(statement)
        return _qualified_name(table) if table is not None else None

    def _reject_duplicate(self, name: str, origin: Origin) -> bool:
        """True, having recorded a diagnostic, if `name` was already declared.

        Which declaration governs is unknown, so the relation contributes no
        Knowledge at all rather than the first declaration's.
        """
        if name.lower() not in self._definitions:
            return False

        self._declared = [known for known in self._declared if known.name.lower() != name.lower()]
        self._diagnostics.append(
            Diagnostic(diag.DUPLICATE_DECLARATION, f"relation {name} is declared more than once", origin)
        )
        return True

    def _register_table_knowledge(self, name: str, statement: exp.Create) -> None:
        # A view's relational plan arrives with a later slice. Until then it
        # declares a relation with no properties, so joins against it stay
        # UNKNOWN rather than being approximated.
        if (statement.kind or "").upper() == "TABLE":
            self._declared.append(self._table_knowledge(name, statement))

    def _register_view_body(self, name: str, statement: exp.Create) -> None:
        if (statement.kind or "").upper() != "VIEW":
            return
        body = statement.args.get("expression")
        if body is not None:
            self._views.declare(name.lower(), body)

    def _table_knowledge(self, name: str, statement: exp.Create) -> RelationKnowledge:
        columns: list[ColumnKnowledge] = []
        unique_sets: list[UniqueSetKnowledge] = []
        non_null: set[str] = set()

        for definition in statement.this.find_all(exp.ColumnDef):
            kinds = [type(constraint.args.get("kind")) for constraint in definition.constraints]
            primary_key = exp.PrimaryKeyColumnConstraint in kinds
            unique = exp.UniqueColumnConstraint in kinds
            not_null = primary_key or exp.NotNullColumnConstraint in kinds
            if not_null:
                non_null.add(definition.name.lower())

            columns.append(ColumnKnowledge(definition.name, nullable=not not_null))
            if primary_key or unique:
                unique_sets.append(UniqueSetKnowledge((definition.name,)))

        # A table-level PRIMARY KEY or UNIQUE constraint declares a composite
        # Unique Set; a PRIMARY KEY additionally makes every member non-null.
        for constraint in statement.this.expressions:
            table_level_columns = _table_level_unique_columns(constraint)
            if table_level_columns is None:
                continue
            unique_sets.append(UniqueSetKnowledge(table_level_columns))
            if isinstance(constraint, exp.PrimaryKey):
                non_null.update(column.lower() for column in table_level_columns)

        if non_null:
            columns = [
                replace(column, nullable=False) if column.name.lower() in non_null else column
                for column in columns
            ]

        return RelationKnowledge(
            name=name,
            columns=tuple(columns),
            unique_sets=tuple(unique_sets),
            origin=Origin(SQL, statement.sql(dialect=self.dialect)),
        )

    def _register_definition(self, name: str, origin: Origin) -> ir.RelationDefinition:
        key = name.lower()
        if key not in self._definitions:
            self._definitions[key] = ir.RelationDefinition(
                id=self.names.new(naming.RELATION, name),
                name=name,
                origin_id=self.origins.register(origin),
            )
        return self._definitions[key]

    # Definition pass ----------------------------------------------------------

    def _lower_query(self, select: exp.Query) -> ir.Plan | None:
        # A CTE name must never resolve to a declared relation of the same
        # name, or it would borrow that relation's Unique Sets. Its body
        # lowers on first reference, through the same recursive path a FROM
        # subquery uses -- see `_lower_cte_reference`.
        if not select: return None
        with_clause = _arg(select, "with")
        if with_clause is not None:
            for cte in with_clause.expressions:
                self._ctes.declare(cte.alias.lower(), cte.this)

        source = _from_source(select)
        if source is None:
            return None

        plan = self._lower_source(source)
        for join in select.args.get("joins") or []:
            plan = self._lower_join(plan, join)
        return plan

    def _lower_source(self, source: exp.Expression) -> ir.Plan:
        """Every unsupported or exhausted attempt below falls through to the
        same OpaqueRelation, so callers never see a bare `None`."""
        origin_id = self._origin_id(source)
        nested = self._lower_nested_source(source, origin_id)
        return nested if nested is not None else self._opaque_relation(source, origin_id)

    def _lower_nested_source(self, source: exp.Expression, origin_id: str) -> ir.Plan | None:
        if isinstance(source, exp.Table):
            name = _qualified_name(source).lower()
            if self._ctes.shadows(name):
                return self._lower_cte_reference(source, name, origin_id)
            return self._lower_table_reference(source, name, origin_id)
        if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
            alias = source.alias_or_name
            if alias:
                return self._lower_nested_select(source.this, alias, origin_id)
        return None

    def _lower_table_reference(self, table: exp.Table, name: str, origin_id: str) -> ir.Plan:
        """A bare table, or one reference to a Create View, lowered fresh.

        A view's body lowers through the same recursive path a CTE or FROM
        subquery uses, so a view over a table, another view, or a nested
        subquery all resolve the same way regardless of how deep the nesting
        goes -- and each reference re-lowers the body independently, so two
        uses of the same view get separate Relation Instances exactly like two
        aliases of a table (`_scan` does the same). A name with no known view
        body -- because it is a plain table, or its body could not be
        recorded -- falls back to a Scan, as does a body this slice cannot
        lower (a JOIN, GROUP BY, DISTINCT, or QUALIFY inside it) and a
        reference caught mid-cycle: in every case the relation still exists,
        it just has no Knowledge to prove a join from.
        """
        body = self._views.body(name)
        if body is None:
            return self._scan(table, origin_id)

        if self._views.is_resolving(name):
            self._report_cycle(table, name)
            return self._scan(table, origin_id)

        alias = table.alias or table.name
        with self._views.resolving(name):
            plan = self._lower_nested_select(body, alias, origin_id)

        if plan is None:
            return self._scan(table, origin_id)
        return self._rebind_definition(plan, self._definitions[name].id)

    def _report_cycle(self, table: exp.Table, name: str) -> None:
        self._diagnostics.append(
            Diagnostic(
                diag.RECURSIVE_VIEW_DEFINITION,
                f"view {name} is defined recursively, which is not supported",
                Origin(SQL, table.sql(dialect=self.dialect)),
            )
        )

    def _rebind_definition(self, plan: ir.Plan, definition_id: str) -> ir.Plan:
        """The same Plan, with its outer Relation Instance's identity bound to
        `definition_id` instead of the anonymous one `_filter`/`_project` gave it.

        A view's own Relation Definition is registered up front, in the
        declaration pass, so an expansion of its body should carry that real
        identity rather than an anonymous placeholder -- the same distinction
        `_scan` preserves for a plain table.
        """
        return replace(plan, instance=replace(plan.instance, definition_id=definition_id))

    def _lower_cte_reference(self, table: exp.Table, name: str, origin_id: str) -> ir.Plan | None:
        """One reference to a CTE, lowered fresh from its body.

        Each reference re-lowers the body independently rather than sharing a
        Plan object, so two references to the same CTE never share identity
        any more than two aliases of a table do (`_scan` does the same for
        physical tables).
        """
        body = self._ctes.body(name)
        if body is None:
            return None

        alias = table.alias or table.name
        with self._ctes.resolving(name):
            return self._lower_nested_select(body, alias, origin_id, report_name=f"CTE_{name}")

    def _scan(self, table: exp.Table, origin_id: str) -> ir.Scan:
        definition = self._register_definition(_qualified_name(table), self.origins.resolve(origin_id))
        alias = table.alias or None
        instance = ir.RelationInstance(
            id=self.names.new(naming.INSTANCE, alias or table.name),
            definition_id=definition.id,
            alias=alias,
            origin_id=origin_id,
        )
        return ir.Scan(self.names.new(naming.PLAN, f"scan {table.name}"), instance)

    def _lower_nested_select(
        self, select: exp.Select, alias: str, origin_id: str, report_name: str | None = None
    ) -> ir.Plan | None:
        """A CTE or FROM-subquery body of the narrow shape this slice models:
        one FROM source, an optional WHERE, and a plain column list or `*`.

        The FROM source lowers through `_lower_source`, so a bare table,
        another CTE, or a nested FROM subquery all resolve recursively the
        same way they would at the top level -- this is how a CTE built from
        another CTE, or one wrapping a FROM subquery, keeps working several
        layers deep.

        Anything else -- a JOIN or a nested WITH inside this body -- returns
        None so the caller falls back to an OpaqueRelation, exactly like
        every other unsupported construct in this module. In particular, a
        JOIN inside a CTE or FROM-subquery body stays unsupported: its own
        asserted joins, if any, are reported by `_report_unanalyzed` rather
        than silently dropped.

        A GROUP BY, DISTINCT, or QUALIFY body is delegated to
        `_lower_aggregate`, `_lower_distinct`, or `_lower_qualify` instead of
        the plain Filter/Project path below, since each earns its own Unique
        Set from grouping, distinctness, or recognized partition
        qualification rather than propagating one from its input.

        `report_name` (set only by `_lower_cte_reference`) labels the body's
        own Relation Definition for `report.facts`. Whichever shape this body
        takes -- Filter, Project, Aggregate, Distinct, or QualifyByPartition --
        it always earns a fresh Relation Definition of its own, so it is
        always safe to apply.
        """
        if any(select.args.get(key) for key in ("with", "joins")):
            return None

        source = _from_source(select)
        if source is None:
            return None

        inner_plan = self._lower_source(source)

        group = select.args.get("group")
        if group is not None:
            return self._lower_aggregate(inner_plan, select, group, alias, origin_id, report_name)
        if select.args.get("distinct") is not None:
            return self._lower_distinct(inner_plan, select, alias, origin_id, report_name)
        qualify = select.args.get("qualify")
        if qualify is not None:
            return self._lower_qualify(inner_plan, select, qualify, alias, origin_id, report_name)

        items = select.expressions
        star_only = len(items) == 1 and isinstance(items[0], exp.Star)
        has_where = select.args.get("where") is not None

        plan = inner_plan
        if has_where or star_only:
            # Only a star-only body ends here: give it `report_name`, since
            # `_project` below is the final node -- and gets it instead -- otherwise.
            plan = self._filter(plan, alias if star_only else None, origin_id, report_name if star_only else None)
        if not star_only:
            plan = self._project(plan, items, alias, origin_id, report_name)
        return plan

    def _lower_aggregate(
        self,
        input_plan: ir.Plan,
        select: exp.Select,
        group: exp.Group,
        alias: str,
        origin_id: str,
        report_name: str | None = None,
    ) -> ir.Plan | None:
        """An ordinary `GROUP BY`: unique by its complete set of Grouping Keys.

        Each Grouping Key must appear, unrenamed-computation, among the
        selected outputs -- otherwise there is no output column an outer
        query could join against to exercise it, so the whole aggregate is
        left unsupported rather than earning a Unique Set no join could ever
        fully cover. GROUPING SETS, ROLLUP, CUBE, and HAVING are unsupported
        for the same conservative reason: each changes which rows come out in
        ways this slice does not model.
        """
        if select.args.get("having") is not None:
            return None
        if any(group.args.get(key) for key in ("grouping_sets", "rollup", "cube", "totals")):
            return None

        if select.args.get("where") is not None:
            input_plan = self._filter(input_plan, None, origin_id)

        grouping_keys: list[ir.GroupingKey] = []
        for group_expr in group.expressions:
            output = _matching_output(group_expr, select.expressions)
            if output is None:
                return None
            grouping_keys.append(ir.GroupingKey(output.alias_or_name))

        instance = self._anonymous_instance(alias, origin_id, report_name)
        return ir.Aggregate(self.names.new(naming.PLAN, "aggregate"), input_plan, instance, tuple(grouping_keys), origin_id)

    def _lower_distinct(
        self,
        input_plan: ir.Plan,
        select: exp.Select,
        alias: str,
        origin_id: str,
        report_name: str | None = None,
    ) -> ir.Plan | None:
        """`SELECT DISTINCT`: unique by its complete set of output expressions.

        `DISTINCT ON` and `SELECT DISTINCT *` are unsupported: the former
        keeps only one row per its own key rather than the whole output, and
        the latter has no concrete output list this slice can name a Unique
        Set's members from.
        """
        if select.args.get("distinct").args.get("on") is not None:
            return None
        if select.args.get("having") is not None:
            return None

        items = select.expressions
        if len(items) == 1 and isinstance(items[0], exp.Star):
            return None

        if select.args.get("where") is not None:
            input_plan = self._filter(input_plan, None, origin_id)

        scope = ir.instances(input_plan)
        instance = self._anonymous_instance(alias, origin_id, report_name)
        outputs = self._projected_columns(items, scope)
        return ir.Distinct(self.names.new(naming.PLAN, "distinct"), input_plan, instance, outputs, origin_id)

    def _lower_qualify(
        self,
        input_plan: ir.Plan,
        select: exp.Select,
        qualify: exp.Qualify,
        alias: str,
        origin_id: str,
        report_name: str | None = None,
    ) -> ir.Plan | None:
        """A recognized `ROW_NUMBER() OVER (PARTITION BY ...) = 1` QUALIFY:
        unique by its complete Partition Key, since that predicate keeps
        exactly one row per partition.

        Any other rank function, any predicate other than `= 1`, and a window
        with no `PARTITION BY` are all left unsupported for the same
        conservative reason Aggregate's unsupported shapes are: none of them
        is a case this slice's semantics actually cover. Each Partition Key
        must appear, unrenamed-computation, among the selected outputs --
        otherwise there is no output column an outer query could join against
        to exercise it -- exactly like an Aggregate's Grouping Keys.
        """
        if select.args.get("having") is not None:
            return None

        window = _row_number_equals_one(qualify.this)
        if window is None:
            return None

        partition_by = window.args.get("partition_by") or []
        if not partition_by:
            return None

        if select.args.get("where") is not None:
            input_plan = self._filter(input_plan, None, origin_id)

        scope = ir.instances(input_plan)
        partition_keys: list[ir.PartitionKey] = []
        for partition_expr in partition_by:
            output = _matching_output(partition_expr, select.expressions)
            if output is None:
                return None
            partition_keys.append(
                ir.PartitionKey(output.alias_or_name, self._lower_expression(partition_expr, scope))
            )

        order = window.args.get("order")
        ordering = tuple(
            self._lower_expression(ordered.this, scope) for ordered in (order.expressions if order else [])
        )

        instance = self._anonymous_instance(alias, origin_id, report_name)
        return ir.QualifyByPartition(
            self.names.new(naming.PLAN, "qualify"),
            input_plan,
            instance,
            tuple(partition_keys),
            ordering,
            origin_id,
        )

    def _filter(
        self, input_plan: ir.Plan, alias: str | None, origin_id: str, report_name: str | None = None
    ) -> ir.Filter:
        instance = self._anonymous_instance(alias, origin_id, report_name)
        return ir.Filter(self.names.new(naming.PLAN, "filter"), input_plan, instance, origin_id)

    def _project(
        self,
        input_plan: ir.Plan,
        items: list[exp.Expression],
        alias: str,
        origin_id: str,
        report_name: str | None = None,
    ) -> ir.Project:
        scope = ir.instances(input_plan)
        instance = self._anonymous_instance(alias, origin_id, report_name)
        outputs = self._projected_columns(items, scope)
        return ir.Project(self.names.new(naming.PLAN, "project"), input_plan, instance, outputs, origin_id)

    def _anonymous_instance(
        self, alias: str | None, origin_id: str, report_name: str | None = None
    ) -> ir.RelationInstance:
        """A fresh Relation Instance backed by a nameless Relation Definition,
        for any derived table -- Filter, Project, Aggregate, or Distinct.

        `report_name` labels that Definition for `report.facts` -- a CTE's
        own declared name, say -- without touching `name`, which stays empty
        so Knowledge can never attach to a derived relation by coincidence.
        Safe here specifically because this Definition is minted fresh on
        every call and never shared with anything else.
        """
        hint = alias or "relation"
        definition = ir.RelationDefinition(
            id=self.names.new(naming.RELATION, hint),
            name="",
            origin_id=origin_id,
            report_name=report_name,
        )
        self._anonymous.append(definition)
        return ir.RelationInstance(
            id=self.names.new(naming.INSTANCE, hint),
            definition_id=definition.id,
            alias=alias,
            origin_id=origin_id,
        )

    def _projected_columns(
        self, items: list[exp.Expression], scope: tuple[ir.RelationInstance, ...]
    ) -> tuple[ir.ProjectedColumn, ...]:
        return tuple(
            ir.ProjectedColumn(
                name=item.alias_or_name,
                expression=self._lower_expression(item.this if isinstance(item, exp.Alias) else item, scope),
            )
            for item in items
        )

    def _opaque_relation(self, source: exp.Expression, origin_id: str) -> ir.OpaqueRelation:
        """A subplan this slice does not model: a CTE, a FROM subquery, a set operation.

        Its Relation Definition is deliberately nameless, so no Knowledge can
        attach to it and no proof can be borrowed from a same-named relation.
        """
        hint = source.alias_or_name or "opaque"
        definition = ir.RelationDefinition(
            id=self.names.new(naming.RELATION, hint),
            name="",
            origin_id=origin_id,
        )
        self._anonymous.append(definition)

        instance = ir.RelationInstance(
            id=self.names.new(naming.INSTANCE, hint),
            definition_id=definition.id,
            alias=source.alias_or_name or None,
            origin_id=origin_id,
        )
        return ir.OpaqueRelation(
            id=self.names.new(naming.PLAN, "opaque"),
            description=source.sql(dialect=self.dialect),
            instance=instance,
            origin_id=origin_id,
        )

    def _lower_join(self, left: ir.Plan, join: exp.Join) -> ir.Join:
        origin_id = self._origin_id(join, assertion_line(join))
        right = self._lower_source(join.this)

        lowered = ir.Join(
            id=self.names.new(naming.JOIN, _join_hint(join)),
            kind=_join_kind(join),
            left=left,
            right=right,
            equalities=self._lower_join_predicate(join, left, right),
            origin_id=origin_id,
        )

        if assertion_line(join) is not None:
            self._assertions.append(
                ir.UniqueJoinAssertion(
                    id=self.names.new(naming.ASSERTION, _join_hint(join)),
                    join_id=lowered.id,
                    origin_id=self.origins.register(join_origin(join, self.dialect)),
                )
            )
        return lowered

    def _lower_join_predicate(self, join: exp.Join, left: ir.Plan, right: ir.Plan) -> tuple[ir.Equality, ...]:
        using = join.args.get("using")
        if using:
            return tuple(self._lower_using_equality(identifier, left, right) for identifier in using)
        scope = ir.instances(left) + ir.instances(right)
        return self._lower_predicate(join.args.get("on"), scope)

    def _lower_using_equality(self, identifier: exp.Identifier, left: ir.Plan, right: ir.Plan) -> ir.Equality:
        """`USING (col)` means `left.col = right.col`.

        Each side resolves only within its own plan's instances: an
        unqualified column resolves when its side has exactly one instance,
        and stays opaque otherwise. Nothing here needs a matching relation's
        actual columns, so USING is sound even with no catalog knowledge.
        """
        name = identifier.name
        return ir.Equality(
            id=self.names.new(naming.EXPRESSION, "equality"),
            left=self._lower_expression(exp.column(name), ir.instances(left)),
            right=self._lower_expression(exp.column(name), ir.instances(right)),
            origin_id=self._origin_id(identifier),
        )

    def _lower_predicate(
        self,
        predicate: exp.Expression | None,
        scope: tuple[ir.RelationInstance, ...],
    ) -> tuple[ir.Equality, ...]:
        if predicate is None:
            return ()

        conjuncts = list(predicate.flatten()) if isinstance(predicate, exp.And) else [predicate]
        return tuple(
            ir.Equality(
                id=self.names.new(naming.EXPRESSION, "equality"),
                left=self._lower_expression(conjunct.this, scope),
                right=self._lower_expression(conjunct.expression, scope),
                origin_id=self._origin_id(conjunct),
            )
            for conjunct in conjuncts
            if isinstance(conjunct, exp.EQ)
        )

    def _lower_expression(self, expression: exp.Expression, scope: tuple[ir.RelationInstance, ...]) -> ir.Expression:
        origin_id = self._origin_id(expression)

        if isinstance(expression, exp.Column):
            instance = self._resolve(expression, scope)
            if instance is not None:
                return ir.ColumnReference(
                    id=self.names.new(naming.COLUMN, f"{instance.alias or ''} {expression.name}"),
                    instance_id=instance.id,
                    column=expression.name,
                    origin_id=origin_id,
                )

        if isinstance(expression, exp.Literal):
            return ir.Constant(id=self.names.new(naming.EXPRESSION, "const"), origin_id=origin_id)

        return ir.OpaqueExpression(
            id=self.names.new(naming.EXPRESSION, "opaque"),
            description=expression.sql(dialect=self.dialect),
            origin_id=origin_id,
        )

    def _resolve(self, column: exp.Column, scope: tuple[ir.RelationInstance, ...]) -> ir.RelationInstance | None:
        """The instance a column reference names, or None when it is ambiguous."""
        if not column.table:
            return scope[0] if len(scope) == 1 else None

        qualifier = column.table.lower()
        matches = [instance for instance in scope if self._identifies(instance, qualifier)]
        return matches[0] if len(matches) == 1 else None

    def _identifies(self, instance: ir.RelationInstance, qualifier: str) -> bool:
        if instance.alias is not None:
            return instance.alias.lower() == qualifier

        definition = next(
            (
                candidate
                for candidate in (*self._definitions.values(), *self._anonymous)
                if candidate.id == instance.definition_id
            ),
            None,
        )
        if definition is None or not definition.name:
            return False
        return definition.name.lower().rsplit(".", 1)[-1] == qualifier

    def _origin_id(self, node: exp.Expression, line: int | None = None) -> str:
        return self.origins.register(Origin(SQL, node.sql(dialect=self.dialect), line))


def _asserted_joins(ast: ParsedProgram) -> list[exp.Join]:
    statements = (*ast.create_statements, ast.root_select)
    return [
        join
        for statement in statements
        if statement is not None
        for join in statement.find_all(exp.Join)
        if assertion_line(join) is not None
    ]


def _table_level_unique_columns(constraint: exp.Expression) -> tuple[str, ...] | None:
    """Column names of a table-level `PRIMARY KEY` or `UNIQUE` constraint.

    `None` if `constraint` is not one of these; inline column constraints are
    handled separately, from each `ColumnDef`.
    """
    if isinstance(constraint, exp.PrimaryKey):
        return tuple(identifier.name for identifier in constraint.expressions)
    if isinstance(constraint, exp.UniqueColumnConstraint) and isinstance(constraint.this, exp.Schema):
        return tuple(identifier.name for identifier in constraint.this.expressions)
    return None


def _created_table(statement: exp.Create) -> exp.Table | None:
    target = statement.this
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None


def _qualified_name(table: exp.Table) -> str:
    """The whole name of a relation: `users` and `b.users` are different relations."""
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _arg(node: exp.Expression, name: str) -> exp.Expression | None:
    """Read a SQLGlot argument, tolerating the trailing-underscore key names."""
    return node.args.get(f"{name}_") or node.args.get(name)


def _from_source(select: exp.Query) -> exp.Expression | None:
    source = _arg(select, "from")
    return source.this if isinstance(source, exp.From) else None


def _matching_output(group_expr: exp.Expression, items: list[exp.Expression]) -> exp.Expression | None:
    """The selected output item computing `group_expr` unrenamed, if any.

    A Grouping Key with no matching output has no column an outer query could
    join against, so this signals the caller to leave the aggregate
    unsupported instead.
    """
    return next(
        (item for item in items if (item.this if isinstance(item, exp.Alias) else item) == group_expr),
        None,
    )


def _row_number_equals_one(predicate: exp.Expression) -> exp.Window | None:
    """The `ROW_NUMBER() OVER (...)` window this QUALIFY predicate retains
    exactly one row per partition from, if it is recognized.

    Only `<window> = 1` is recognized: any other predicate shape, or a window
    function other than `ROW_NUMBER`, could retain more than one row per
    partition and must never be mistaken for uniqueness.
    """
    if not isinstance(predicate, exp.EQ):
        return None

    left, right = predicate.this, predicate.expression
    window = left if isinstance(left, exp.Window) else right if isinstance(right, exp.Window) else None
    literal = right if window is left else left
    if window is None or not isinstance(window.this, exp.RowNumber):
        return None
    if not isinstance(literal, exp.Literal) or literal.is_string or literal.this != "1":
        return None
    return window


def _join_kind(join: exp.Join) -> str:
    side = (join.args.get("side") or "").lower()
    kind = (join.args.get("kind") or "").lower()
    return side or kind or ir.INNER


def _join_hint(join: exp.Join) -> str:
    return getattr(join.this, "alias_or_name", "") or "join"
