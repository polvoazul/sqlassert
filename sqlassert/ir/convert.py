"""Link a parsed SQL program into the framework-independent relational IR.

Declaration and scope discovery happens before lowering. This gives table,
view, and CTE references compiler-like symbol resolution: every occurrence is
a fresh Alias, while all occurrences share one memoized NamedRelation.
"""

from __future__ import annotations

from collections.abc import Iterable, Set
from dataclasses import dataclass, field

from sqlglot import exp

from sqlassert import diagnostics as diag
from sqlassert import ir
from sqlassert.diagnostics import Diagnostic
from sqlassert.knowledge import Knowledge, NonNullColumn, UniqueSet
from sqlassert.provenance import Origin, SQL
from sqlassert.sql_parse import ParsedProgram, assertion_line, join_origin, unique_set_assertions


@dataclass(frozen=True)
class IrConversionResult:
    program: ir.Program
    knowledge: tuple[Knowledge, ...]
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(eq=False)
class _Symbol:
    name: str
    role: ir.RelationRole
    origin: Origin
    body: exp.Query | None = None
    statement: exp.Create | None = None
    required_columns: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _Binding:
    qualifier: str
    name: str
    column: ir.OutputColumn


@dataclass(frozen=True)
class _Lowered:
    relation: ir.RelationExpr
    bindings: tuple[_Binding, ...]


@dataclass
class IrParser:
    dialect: str
    _global_symbols: dict[str, _Symbol] = field(default_factory=dict)
    _symbols: list[_Symbol] = field(default_factory=list)
    _query_scopes: dict[int, dict[str, _Symbol]] = field(default_factory=dict)
    _table_symbols: dict[int, _Symbol] = field(default_factory=dict)
    _source_required: dict[int, set[str]] = field(default_factory=dict)
    _source_expressions: dict[int, exp.Expression] = field(default_factory=dict)
    _named_relations: dict[_Symbol, ir.NamedRelation] = field(default_factory=dict)
    _resolving: set[_Symbol] = field(default_factory=set)
    _knowledge: list[Knowledge] = field(default_factory=list)
    _assertions: list[ir.Assertion] = field(default_factory=list)
    _diagnostics: list[Diagnostic] = field(default_factory=list)
    _handled_unique_set_lines: set[int] = field(default_factory=set)
    _reported_cycles: set[_Symbol] = field(default_factory=set)
    def parse(self, ast: ParsedProgram) -> IrConversionResult:
        for statement in ast.create_statements:
            if isinstance(statement, exp.Create):
                self._declare(statement)

        for symbol in tuple(self._symbols):
            if symbol.role is ir.RelationRole.VIEW and symbol.body is not None:
                self._discover_query(symbol.body, {})
        if ast.root_select is not None:
            self._discover_query(ast.root_select, {})
        self._propagate_required_columns()

        root = self._lower_query(ast.root_select, root=True) if ast.root_select is not None else None
        self._report_unanalyzed(ast)

        declarations = tuple(self._named_relations.get(symbol) or self._unresolved_named_relation(symbol) for symbol in self._symbols)
        return IrConversionResult(ir.Program(declarations=declarations, root=root, assertions=tuple(self._assertions)), tuple(self._knowledge), tuple(self._diagnostics))

    # Declaration and symbol discovery ---------------------------------------

    def _declare(self, statement: exp.Create) -> None:
        kind = (statement.kind or "").upper()
        if kind not in _SUPPORTED_CREATE_KINDS:
            self._diagnostics.append(
                Diagnostic(
                    diag.UNSUPPORTED_CREATE_STATEMENT,
                    f"create statement kind {kind or '<unknown>'} is not supported; only TABLE and VIEW are analyzed: {statement.sql(dialect=self.dialect)}",
                )
            )
            return

        table = _created_table(statement)
        if table is None:
            return
        name = _qualified_name(table)
        key = name.lower()
        origin = Origin(SQL, statement.sql(dialect=self.dialect))
        if key in self._global_symbols:
            self._diagnostics.append(Diagnostic(diag.DUPLICATE_DECLARATION, f"relation {name} is declared more than once", origin))
            return

        role = ir.RelationRole.TABLE if kind == "TABLE" else ir.RelationRole.VIEW
        body = statement.args.get("expression")
        query = _unwrap_query(body)
        symbol = _Symbol(name=name, role=role, origin=origin, body=query, statement=statement)
        self._global_symbols[key] = symbol
        self._symbols.append(symbol)

    def _discover_query(self, query: exp.Query, inherited_scope: dict[str, _Symbol]) -> None:
        if id(query) in self._query_scopes:
            return

        scope = dict(inherited_scope)
        with_clause = _arg(query, "with")
        if with_clause is not None:
            for cte in with_clause.expressions:
                body = _unwrap_query(cte.this)
                symbol = _Symbol(
                    name=cte.alias,
                    role=ir.RelationRole.CTE,
                    origin=Origin(SQL, cte.sql(dialect=self.dialect)),
                    body=body,
                )
                scope[cte.alias.lower()] = symbol
                self._symbols.append(symbol)

        self._query_scopes[id(query)] = scope
        if isinstance(query, exp.SetOperation):
            self._discover_query(query.this, scope)
            self._discover_query(query.expression, scope)
            return
        if not isinstance(query, exp.Select):
            return

        sources = _direct_sources(query)
        by_qualifier: dict[str, list[exp.Expression]] = {}
        for source in sources:
            self._source_required.setdefault(id(source), set())
            self._source_expressions[id(source)] = source
            qualifier = source.alias_or_name.lower()
            if qualifier:
                by_qualifier.setdefault(qualifier, []).append(source)
            if isinstance(source, exp.Table):
                symbol = self._symbol_for_table(source, scope)
                self._table_symbols[id(source)] = symbol
            elif isinstance(source, exp.Subquery):
                body = _unwrap_query(source)
                if body is not None:
                    self._discover_query(body, scope)

        for column in query.find_all(exp.Column):
            if column.table:
                matching = by_qualifier.get(column.table.lower(), ())
                for source in matching:
                    self._require(source, column.name)
            elif len(sources) == 1:
                self._require(sources[0], column.name)

        for join in query.args.get("joins") or ():
            for identifier in join.args.get("using") or ():
                for source in sources:
                    self._require(source, identifier.name)

        if len(sources) == 1 and _star_only(query):
            for _, columns, _ in unique_set_assertions(query):
                for column in columns:
                    self._require(sources[0], column)

        if with_clause is not None:
            for cte in with_clause.expressions:
                body = _unwrap_query(cte.this)
                if body is not None:
                    self._discover_query(body, scope)

    def _symbol_for_table(self, table: exp.Table, scope: dict[str, _Symbol]) -> _Symbol:
        name = _qualified_name(table)
        key = name.lower()
        if key in scope:
            return scope[key]
        symbol = self._global_symbols.get(key)
        if symbol is None:
            symbol = _Symbol(name=name, role=ir.RelationRole.TABLE, origin=Origin(SQL, table.sql(dialect=self.dialect)))
            self._global_symbols[key] = symbol
            self._symbols.append(symbol)
        return symbol

    def _require(self, source: exp.Expression, column: str) -> None:
        self._source_required.setdefault(id(source), set()).add(column)
        if isinstance(source, exp.Table):
            self._table_symbols[id(source)].required_columns.add(column)

    def _propagate_required_columns(self) -> None:
        """Push requested output columns through star-only view/CTE/subquery bodies."""
        changed = True
        while changed:
            before = sum(len(symbol.required_columns) for symbol in self._symbols) + sum(len(columns) for columns in self._source_required.values())
            for symbol in self._symbols:
                if isinstance(symbol.body, exp.Select) and _star_only(symbol.body):
                    sources = _direct_sources(symbol.body)
                    if len(sources) == 1:
                        for column in symbol.required_columns:
                            self._require(sources[0], column)
            for source_id, columns in tuple(self._source_required.items()):
                source = self._source_expressions[source_id]
                body = _unwrap_query(source) if isinstance(source, exp.Subquery) else None
                if isinstance(body, exp.Select) and _star_only(body):
                    nested_sources = _direct_sources(body)
                    if len(nested_sources) == 1:
                        for column in columns:
                            self._require(nested_sources[0], column)
            after = sum(len(symbol.required_columns) for symbol in self._symbols) + sum(len(columns) for columns in self._source_required.values())
            changed = after != before

    # Lowering ---------------------------------------------------------------

    def _lower_query(self, query: exp.Query, *, root: bool = False) -> ir.RelationExpr | None:
        unwrapped = _unwrap_query(query)
        if unwrapped is None:
            return None
        query = unwrapped
        if isinstance(query, exp.SetOperation):
            left = self._lower_query(query.this, root=True)
            right = self._lower_query(query.expression, root=True)
            if left is None:
                left = self._opaque_relation(query.this)
            if right is None:
                right = self._opaque_relation(query.expression)
            output_columns = _pass_output_columns(left.output_columns, self._origin(query))
            return ir.SetOperation(
                origin=self._origin(query), output_columns=output_columns, is_schema_complete=left.is_schema_complete and right.is_schema_complete,
                operator=query.key, left=left, right=right,
            )
        if not isinstance(query, exp.Select):
            return None
        return self._lower_select(query, allow_joins=root)

    def _lower_select(self, select: exp.Select, *, allow_joins: bool) -> ir.RelationExpr | None:
        if not allow_joins and any(select.args.get(key) for key in ("with", "joins")):
            return None
        source = _from_source(select)
        if source is None:
            return None

        lowered = self._lower_source(source)
        if allow_joins:
            for join in select.args.get("joins") or ():
                lowered = self._lower_join(lowered, join)

        relation = self._lower_select_tail(lowered, select)
        self._register_unique_set_assertions(relation, select)
        return relation

    def _lower_source(self, source: exp.Expression) -> _Lowered:
        if isinstance(source, exp.Table):
            symbol = self._table_symbols[id(source)]
            alias_name = source.alias or source.name
            if symbol in self._resolving:
                if symbol.role is ir.RelationRole.VIEW:
                    self._report_cycle(symbol, source)
                recursive = self._recursive_relation(symbol, source)
                return self._alias(recursive, alias_name, self._origin(source))
            return self._alias(self._resolve_symbol(symbol), alias_name, self._origin(source))

        body = _unwrap_query(source) if isinstance(source, exp.Subquery) else None
        if isinstance(source, exp.Subquery) and source.alias and isinstance(body, exp.Select):
            relation = self._lower_select(body, allow_joins=False)
            if relation is not None:
                return self._alias(relation, source.alias, self._origin(source))

        opaque = self._opaque_relation(source, self._source_required.get(id(source), set()))
        return self._alias(opaque, source.alias_or_name or "opaque", self._origin(source))

    def _resolve_symbol(self, symbol: _Symbol) -> ir.NamedRelation:
        existing = self._named_relations.get(symbol)
        if existing is not None:
            return existing

        if symbol.role is ir.RelationRole.TABLE:
            output_columns, complete = self._table_output_columns(symbol)
            named = ir.NamedRelation(
                origin=symbol.origin, output_columns=output_columns, is_schema_complete=complete, name=symbol.name, role=symbol.role
            )
            self._named_relations[symbol] = named
            self._knowledge.extend(self._table_knowledge(named, symbol.statement))
            return named

        self._resolving.add(symbol)
        try:
            body = self._lower_query(symbol.body, root=False) if symbol.body is not None else None
        finally:
            self._resolving.discard(symbol)
        if body is None:
            body = self._opaque_relation(symbol.body or exp.Table(this=symbol.name), symbol.required_columns)
        output_columns = _pass_output_columns(body.output_columns, symbol.origin)
        named = ir.NamedRelation(
            origin=symbol.origin,
            output_columns=output_columns,
            is_schema_complete=body.is_schema_complete,
            name=symbol.name,
            role=symbol.role,
            body=body,
        )
        self._named_relations[symbol] = named
        return named

    def _unresolved_named_relation(self, symbol: _Symbol) -> ir.NamedRelation:
        output_columns, complete = self._table_output_columns(symbol) if symbol.role is ir.RelationRole.TABLE else (self._opaque_output_columns(symbol.required_columns, symbol.origin), False)
        named = ir.NamedRelation(
            origin=symbol.origin, output_columns=output_columns, is_schema_complete=complete, name=symbol.name, role=symbol.role
        )
        self._named_relations[symbol] = named
        if symbol.role is ir.RelationRole.TABLE:
            self._knowledge.extend(self._table_knowledge(named, symbol.statement))
        return named

    def _table_output_columns(self, symbol: _Symbol) -> tuple[tuple[ir.OutputColumn, ...], bool]:
        names = [column.name for column in symbol.statement.this.find_all(exp.ColumnDef)] if symbol.statement is not None else []
        seen = {name.lower() for name in names}
        names.extend(column for column in sorted(symbol.required_columns, key=str.lower) if column.lower() not in seen)
        complete = symbol.statement is not None and {column.lower() for column in symbol.required_columns} <= seen
        return self._opaque_output_columns(names, symbol.origin), complete

    def _opaque_output_columns(self, names: Iterable[str], origin: Origin) -> tuple[ir.OutputColumn, ...]:
        if isinstance(names, Set):
            names = sorted(names, key=lambda name: (name.lower(), name))
        return tuple(
            ir.OutputColumn(
                origin=origin,
                name=name,
                expression=ir.OpaqueExpression(origin=origin, description=f"source column {name}"),
            )
            for name in names
        )

    def _recursive_relation(self, symbol: _Symbol, source: exp.Expression) -> ir.RecursiveRelation:
        origin = self._origin(source)
        return ir.RecursiveRelation(
            origin=origin,
            output_columns=self._opaque_output_columns(symbol.required_columns, origin),
            is_schema_complete=False,
            description=source.sql(dialect=self.dialect),
            relation_name=symbol.name,
        )

    def _alias(self, source: ir.RelationExpr, name: str, origin: Origin) -> _Lowered:
        output_columns = _pass_output_columns(source.output_columns, origin)
        alias = ir.Alias(origin=origin, output_columns=output_columns, is_schema_complete=source.is_schema_complete, source=source, name=name)
        return _Lowered(alias, tuple(_Binding(name.lower(), output.name.lower(), output) for output in output_columns))

    def _lower_join(self, left: _Lowered, join: exp.Join) -> _Lowered:
        right = self._lower_source(join.this)
        origin = self._origin(join, assertion_line(join))
        equalities = self._lower_join_predicate(join, left, right)
        input_columns = left.relation.output_columns + right.relation.output_columns
        output_columns = _pass_output_columns(input_columns, origin)
        remapped = {source: output for source, output in zip(input_columns, output_columns)}
        relation = ir.Join(
            origin=origin,
            output_columns=output_columns,
            is_schema_complete=left.relation.is_schema_complete and right.relation.is_schema_complete,
            kind=_join_kind(join),
            left=left.relation,
            right=right.relation,
            equalities=equalities,
        )
        bindings = tuple(
            _Binding(binding.qualifier, binding.name, remapped[binding.column])
            for binding in left.bindings + right.bindings
        )
        if assertion_line(join) is not None:
            self._assertions.append(ir.UniqueJoinAssertion(origin=join_origin(join, self.dialect), subject=relation))
        return _Lowered(relation, bindings)

    def _lower_join_predicate(self, join: exp.Join, left: _Lowered, right: _Lowered) -> tuple[ir.Equality, ...]:
        using = join.args.get("using")
        if using:
            return tuple(
                ir.Equality(
                    origin=self._origin(identifier),
                    left=self._lower_expression(exp.column(identifier.name), left.bindings),
                    right=self._lower_expression(exp.column(identifier.name), right.bindings),
                )
                for identifier in using
            )
        predicate = join.args.get("on")
        if predicate is None:
            return ()
        conjuncts = list(predicate.flatten()) if isinstance(predicate, exp.And) else [predicate]
        bindings = left.bindings + right.bindings
        return tuple(
            ir.Equality(
                origin=self._origin(conjunct),
                left=self._lower_expression(conjunct.this, bindings),
                right=self._lower_expression(conjunct.expression, bindings),
            )
            for conjunct in conjuncts
            if isinstance(conjunct, exp.EQ)
        )

    def _lower_select_tail(self, lowered: _Lowered, select: exp.Select) -> ir.RelationExpr:
        group = select.args.get("group")
        if isinstance(group, exp.Group):
            relation = self._lower_aggregate(lowered, select, group)
        elif select.args.get("distinct") is not None:
            relation = self._lower_distinct(lowered, select)
        elif isinstance(select.args.get("qualify"), exp.Qualify):
            relation = self._lower_qualify(lowered, select, select.args["qualify"])
        else:
            if select.args.get("where") is not None:
                lowered = self._filter(lowered, self._origin(select.args["where"]))
            if _star_only(select):
                relation = lowered.relation
            else:
                relation = self._project(lowered, select.expressions, self._origin(select))
        return relation if relation is not None else self._opaque_relation(select, _selected_output_names(select))

    def _filter(self, lowered: _Lowered, origin: Origin) -> _Lowered:
        output_columns = _pass_output_columns(lowered.relation.output_columns, origin)
        remapped = {source: output for source, output in zip(lowered.relation.output_columns, output_columns)}
        relation = ir.Filter(
            origin=origin, output_columns=output_columns, is_schema_complete=lowered.relation.is_schema_complete, input=lowered.relation
        )
        bindings = tuple(
            _Binding(binding.qualifier, binding.name, remapped[binding.column]) for binding in lowered.bindings
        )
        return _Lowered(relation, bindings)

    def _project(self, lowered: _Lowered, items: list[exp.Expression], origin: Origin) -> ir.Project:
        output_columns = tuple(
            ir.OutputColumn(
                origin=self._origin(item),
                name=item.alias_or_name,
                expression=self._lower_expression(item.this if isinstance(item, exp.Alias) else item, lowered.bindings),
            )
            for item in items
        )
        return ir.Project(origin=origin, output_columns=output_columns, is_schema_complete=True, input=lowered.relation)

    def _lower_aggregate(self, lowered: _Lowered, select: exp.Select, group: exp.Group) -> ir.Aggregate | None:
        if select.args.get("having") is not None or any(group.args.get(key) for key in ("grouping_sets", "rollup", "cube", "totals")):
            return None
        if select.args.get("where") is not None:
            lowered = self._filter(lowered, self._origin(select.args["where"]))

        grouped_items: set[int] = set()
        for group_expression in group.expressions:
            item = _matching_grouping_output(group_expression, select.expressions)
            if item is None:
                return None
            grouped_items.add(id(item))

        output_columns = self._aggregate_output_columns(select.expressions, lowered.bindings, grouped_items)
        grouping_outputs: list[ir.OutputColumn] = []
        for group_expression in group.expressions:
            item = _matching_grouping_output(group_expression, select.expressions)
            grouping_outputs.append(output_columns[select.expressions.index(item)])
        return ir.Aggregate(
            origin=self._origin(select), output_columns=output_columns, is_schema_complete=True, input=lowered.relation,
            grouping_outputs=tuple(grouping_outputs),
        )

    def _lower_distinct(self, lowered: _Lowered, select: exp.Select) -> ir.Distinct | None:
        distinct = select.args.get("distinct")
        if distinct is None or distinct.args.get("on") is not None or select.args.get("having") is not None or _star_only(select):
            return None
        if select.args.get("where") is not None:
            lowered = self._filter(lowered, self._origin(select.args["where"]))
        return ir.Distinct(
            origin=self._origin(select), output_columns=self._selected_output_columns(select.expressions, lowered.bindings),
            is_schema_complete=True, input=lowered.relation,
        )

    def _lower_qualify(self, lowered: _Lowered, select: exp.Select, qualify: exp.Qualify) -> ir.QualifyByPartition | None:
        if select.args.get("having") is not None:
            return None
        window = _row_number_equals_one(qualify.this)
        if window is None:
            return None
        partitions = window.args.get("partition_by") or ()
        if not partitions:
            return None
        if select.args.get("where") is not None:
            lowered = self._filter(lowered, self._origin(select.args["where"]))

        output_columns = self._selected_output_columns(select.expressions, lowered.bindings)
        partition_outputs: list[ir.OutputColumn] = []
        for partition in partitions:
            item = _matching_output(partition, select.expressions)
            if item is None:
                return None
            partition_outputs.append(output_columns[select.expressions.index(item)])
        order = window.args.get("order")
        ordering = tuple(
            self._lower_expression(ordered.this, lowered.bindings) for ordered in (order.expressions if order else ())
        )
        return ir.QualifyByPartition(
            origin=self._origin(select), output_columns=output_columns, is_schema_complete=True, input=lowered.relation,
            partition_outputs=tuple(partition_outputs), ordering=ordering,
        )

    def _selected_output_columns(self, items: list[exp.Expression], bindings: tuple[_Binding, ...]) -> tuple[ir.OutputColumn, ...]:
        return tuple(
            ir.OutputColumn(
                origin=self._origin(item),
                name=item.alias_or_name,
                expression=self._lower_expression(item.this if isinstance(item, exp.Alias) else item, bindings),
            )
            for item in items
        )

    def _aggregate_output_columns(self, items: list[exp.Expression], bindings: tuple[_Binding, ...], grouped_items: set[int]) -> tuple[ir.OutputColumn, ...]:
        output_columns: list[ir.OutputColumn] = []
        for item in items:
            expression = item.this if isinstance(item, exp.Alias) else item
            lowered = self._lower_expression(expression, bindings)
            if id(item) not in grouped_items and _is_bare_aggregate_expression(expression):
                lowered = ir.AnyAggregate(origin=self._origin(expression), input=lowered)
            output_columns.append(ir.OutputColumn(origin=self._origin(item), name=item.alias_or_name, expression=lowered))
        return tuple(output_columns)

    def _lower_expression(self, expression: exp.Expression, bindings: tuple[_Binding, ...]) -> ir.ScalarExpr:
        origin = self._origin(expression)
        if isinstance(expression, exp.Column):
            if expression.table:
                matches = [binding for binding in bindings if binding.qualifier == expression.table.lower() and binding.name == expression.name.lower()]
            elif len({binding.qualifier for binding in bindings}) == 1:
                matches = [binding for binding in bindings if binding.name == expression.name.lower()]
            else:
                matches = []
            if len(matches) == 1:
                return ir.ColumnRef(origin=origin, column=matches[0].column)
        if isinstance(expression, exp.Literal):
            return ir.Constant(origin=origin)
        return ir.OpaqueExpression(origin=origin, description=expression.sql(dialect=self.dialect))

    def _opaque_relation(self, source: exp.Expression, names=()) -> ir.OpaqueRelation:
        origin = self._origin(source)
        return ir.OpaqueRelation(
            origin=origin,
            output_columns=self._opaque_output_columns(names, origin),
            is_schema_complete=False,
            description=source.sql(dialect=self.dialect),
        )

    # Assertions and diagnostics --------------------------------------------

    def _register_unique_set_assertions(self, relation: ir.RelationExpr, select: exp.Select) -> None:
        by_name = {output.name.lower(): output for output in relation.output_columns}
        for kind, columns, line in unique_set_assertions(select):
            self._handled_unique_set_lines.add(line)
            origin = Origin(SQL, select.sql(dialect=self.dialect), line)
            unknown = [column for column in columns if column.lower() not in by_name]
            if unknown and relation.is_schema_complete:
                self._diagnostics.append(
                    Diagnostic(
                        diag.UNKNOWN_ASSERTED_COLUMN,
                        f"the unique set assertion on line {line} names {', '.join(unknown)}, which is not among this Select Expression's own output columns",
                        origin,
                    )
                )
                continue
            if unknown:
                continue
            self._assertions.append(
                ir.UniqueSetAssertion(
                    origin=origin,
                    subject=relation,
                    columns=tuple(by_name[column.lower()] for column in columns),
                    is_candidate_key=kind == "key",
                )
            )

    def _report_cycle(self, symbol: _Symbol, table: exp.Table) -> None:
        if symbol in self._reported_cycles:
            return
        self._reported_cycles.add(symbol)
        self._diagnostics.append(
            Diagnostic(
                diag.RECURSIVE_VIEW_DEFINITION,
                f"view {symbol.name.lower()} is defined recursively, which is not supported",
                Origin(SQL, table.sql(dialect=self.dialect)),
            )
        )

    def _report_unanalyzed(self, ast: ParsedProgram) -> None:
        analyzed = {assertion.origin.line for assertion in self._assertions if isinstance(assertion, ir.UniqueJoinAssertion)}
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
        for select in _asserted_selects(ast):
            for _, _, line in unique_set_assertions(select):
                if line in self._handled_unique_set_lines:
                    continue
                self._diagnostics.append(
                    Diagnostic(
                        diag.UNANALYZED_ASSERTION,
                        f"the unique set assertion on line {line} is in a part of the program this analysis does not model",
                        Origin(SQL, select.sql(dialect=self.dialect), line),
                    )
                )

    def _table_knowledge(self, relation: ir.NamedRelation, statement: exp.Create | None) -> tuple[Knowledge, ...]:
        if statement is None:
            return ()
        columns = {column.name.lower(): column for column in relation.output_columns}
        facts: list[Knowledge] = []
        unique_sets: list[tuple[str, ...]] = []
        non_null: set[str] = set()
        for definition in statement.this.find_all(exp.ColumnDef):
            kinds = [type(constraint.args.get("kind")) for constraint in definition.constraints]
            primary_key = exp.PrimaryKeyColumnConstraint in kinds
            unique = exp.UniqueColumnConstraint in kinds
            not_null = primary_key or exp.NotNullColumnConstraint in kinds
            if not_null:
                non_null.add(definition.name.lower())
            if primary_key or unique:
                unique_sets.append((definition.name,))
        for constraint in statement.this.expressions:
            constrained = _table_level_unique_columns(constraint)
            if constrained is None:
                continue
            unique_sets.append(constrained)
            if isinstance(constraint, exp.PrimaryKey):
                non_null.update(column.lower() for column in constrained)
        facts.extend(NonNullColumn(column=columns[name]) for name in sorted(non_null) if name in columns)
        for column_names in unique_sets:
            members = tuple(columns[name.lower()] for name in column_names if name.lower() in columns)
            if len(members) != len(column_names):
                continue
            facts.append(UniqueSet(columns=frozenset(members)))
        return tuple(facts)

    def _origin(self, node: exp.Expression, line: int | None = None) -> Origin:
        return Origin(SQL, node.sql(dialect=self.dialect), line)


def _pass_output_columns(inputs: tuple[ir.OutputColumn, ...], origin: Origin) -> tuple[ir.OutputColumn, ...]:
    return tuple(
        ir.OutputColumn(origin=origin, name=column.name, expression=ir.ColumnRef(origin=origin, column=column))
        for column in inputs
    )


def _direct_sources(select: exp.Select) -> list[exp.Expression]:
    source = _from_source(select)
    return ([] if source is None else [source]) + [join.this for join in select.args.get("joins") or ()]


def _asserted_joins(ast: ParsedProgram) -> list[exp.Join]:
    statements = (*ast.create_statements, ast.root_select)
    return [
        join for statement in statements if statement is not None
        for join in statement.find_all(exp.Join) if assertion_line(join) is not None
    ]


def _asserted_selects(ast: ParsedProgram) -> list[exp.Select]:
    statements = (*ast.create_statements, ast.root_select)
    return [
        select for statement in statements if statement is not None
        for select in statement.find_all(exp.Select) if unique_set_assertions(select)
    ]


def _table_level_unique_columns(constraint: exp.Expression) -> tuple[str, ...] | None:
    if isinstance(constraint, exp.PrimaryKey):
        return tuple(identifier.name for identifier in constraint.expressions)
    if isinstance(constraint, exp.UniqueColumnConstraint) and isinstance(constraint.this, exp.Schema):
        return tuple(identifier.name for identifier in constraint.this.expressions)
    return None


_SUPPORTED_CREATE_KINDS = {"TABLE", "VIEW"}


def _created_table(statement: exp.Create) -> exp.Table | None:
    target = statement.this
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None


def _qualified_name(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _unwrap_query(body: exp.Expression | None) -> exp.Query | None:
    while isinstance(body, exp.Subquery):
        body = body.this
    return body if isinstance(body, exp.Query) else None


def _arg(node: exp.Expression, name: str) -> exp.Expression | None:
    return node.args.get(f"{name}_") or node.args.get(name)


def _from_source(select: exp.Query) -> exp.Expression | None:
    source = _arg(select, "from")
    return source.this if isinstance(source, exp.From) else None


def _star_only(select: exp.Select) -> bool:
    return len(select.expressions) == 1 and isinstance(select.expressions[0], exp.Star)


def _selected_output_names(select: exp.Select) -> tuple[str, ...]:
    return tuple(item.alias_or_name for item in select.expressions if not isinstance(item, exp.Star))


def _matching_output(group_expression: exp.Expression, items: list[exp.Expression]) -> exp.Expression | None:
    return next(
        (item for item in items if (item.this if isinstance(item, exp.Alias) else item) == group_expression),
        None,
    )


def _matching_grouping_output(group_expression: exp.Expression, items: list[exp.Expression]) -> exp.Expression | None:
    if isinstance(group_expression, exp.Literal) and group_expression.is_int:
        position = int(group_expression.this)
        return items[position - 1] if 1 <= position <= len(items) else None
    return _matching_output(group_expression, items)


def _is_bare_aggregate_expression(expression: exp.Expression) -> bool:
    return any(isinstance(node, exp.Column) for node in expression.walk()) and not any(
        isinstance(node, exp.AggFunc) for node in expression.walk()
    )


def _row_number_equals_one(predicate: exp.Expression) -> exp.Window | None:
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


_JOIN_KINDS_OVERRIDING_SIDE = {"semi", "anti", "cross"}


def _join_kind(join: exp.Join) -> str:
    kind = (join.args.get("kind") or "").lower()
    if kind in _JOIN_KINDS_OVERRIDING_SIDE:
        return kind
    side = (join.args.get("side") or "").lower()
    return side or kind or ir.INNER
