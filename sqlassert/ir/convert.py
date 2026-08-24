"""Convert a parsed SQL Program into the relational IR and the Knowledge
its Create Statements declare.

This is the only module that reads SQLGlot nodes and writes IR values, so it is
the seam where syntax stops and semantics begin. Conversion is two-pass:
declarations are registered first, then bodies are lowered, so that a definition
may reference one declared later.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

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
    _local_names: set[str] = field(default_factory=set)
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
        # CTEs become relational subplans in a later slice. Until then they are
        # local names that must never resolve to a declared relation of the
        # same name, or they would borrow its Unique Sets.
        if not select: return None
        with_clause = _arg(select, "with")
        if with_clause is not None:
            self._local_names |= {cte.alias.lower() for cte in with_clause.expressions}

        source = _from_source(select)
        if source is None:
            return None

        plan = self._lower_source(source)
        for join in select.args.get("joins") or []:
            plan = self._lower_join(plan, join)
        return plan

    def _lower_source(self, source: exp.Expression) -> ir.Plan:
        origin_id = self._origin_id(source)

        if isinstance(source, exp.Table) and _qualified_name(source).lower() not in self._local_names:
            return self._scan(source, origin_id)
        return self._opaque_relation(source, origin_id)

    def _scan(self, table: exp.Table, origin_id: str) -> ir.Scan:
        definition = self._register_definition(_qualified_name(table), self.origins.resolve(origin_id))
        instance = ir.RelationInstance(
            id=self.names.new(naming.INSTANCE, table.alias or table.name),
            definition_id=definition.id,
            alias=table.alias or None,
            origin_id=origin_id,
        )
        return ir.Scan(self.names.new(naming.PLAN, f"scan {table.name}"), instance)

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


def _join_kind(join: exp.Join) -> str:
    side = (join.args.get("side") or "").lower()
    kind = (join.args.get("kind") or "").lower()
    return side or kind or ir.INNER


def _join_hint(join: exp.Join) -> str:
    return getattr(join.this, "alias_or_name", "") or "join"
