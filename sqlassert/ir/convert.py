"""Link a parsed SQL program into the framework-independent relational IR.

Declaration and scope discovery happens before lowering. This gives table,
view, and CTE references compiler-like symbol resolution: every occurrence is
a fresh Alias, while all occurrences share one memoized NamedRelation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence, Set
from dataclasses import dataclass, field, replace
from functools import cache

from sqlglot import exp

from sqlassert import diagnostics as diag
from sqlassert import ir
from sqlassert.diagnostics import Diagnostic
from sqlassert.properties import (
    EnumeratedDomain, ExactlyOneJoin, ExhaustiveCase, ForeignKey, FunctionalDependency, NonNullColumn, NonNullColumns, NonNullUniqueSet, Property, ReferentialCoverage, UniqueJoin,
    UniqueSet,
)
from sqlassert.provenance import AttachmentSite, Origin, SQL
from sqlassert.sql_parse import (
    FOREIGN_KEY_KINDS, JOIN_MARKER_NOUNS, MARKER_NOUNS, ColumnMarker, ParsedProgram, assertion_line, column_markers,
    exhaustive_line, join_marker_kind, join_origin, literal_value, marker_position,
)


@dataclass(frozen=True)
class IrConversionResult:
    program: ir.Program
    knowledge: tuple[Property, ...]
    diagnostics: tuple[Diagnostic, ...] = ()
    warnings: tuple[Diagnostic, ...] = ()


@dataclass(eq=False)
class _Symbol:
    name: str
    role: ir.RelationRole
    origin: Origin
    body: exp.Query | None = None
    statement: exp.Create | None = None
    required_columns: set[str] = field(default_factory=set)
    # Table-level constraints added after CREATE TABLE (ALTER TABLE, CREATE
    # UNIQUE INDEX), shaped like the CREATE TABLE's own, and columns set NOT NULL.
    constraints: list[exp.Expression] = field(default_factory=list)
    not_null: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _Binding:
    qualifier: str
    name: str
    column: ir.OutputColumn


@dataclass(frozen=True)
class _MarkerReference:
    """A Foreign Key marker whose referencing columns are lowered, awaiting its referenced relation."""

    marker: ColumnMarker
    origin: Origin
    columns: tuple[ir.OutputColumn, ...]
    target: _Symbol
    position: int


@dataclass(frozen=True)
class _ConstraintReference:
    """A `REFERENCES` constraint of a declared table onto a relation the program reaches."""

    table: _Symbol
    columns: tuple[str, ...]
    target: _Symbol
    target_columns: tuple[str, ...] | None
    origin: Origin


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
    _knowledge: list[Property] = field(default_factory=list)
    _assertions: list[ir.Assertion] = field(default_factory=list)
    _declarations: list[Property] = field(default_factory=list)
    _diagnostics: list[Diagnostic] = field(default_factory=list)
    _warnings: list[Diagnostic] = field(default_factory=list)
    _handled_column_markers: set[int] = field(default_factory=set)
    _handled_joins: set[int] = field(default_factory=set)
    _handled_cases: set[int] = field(default_factory=set)
    _reported_cycles: set[_Symbol] = field(default_factory=set)
    _attachment_sites: list[tuple[AttachmentSite, ir.RelationExpr | ir.SimpleCase]] = field(default_factory=list)
    _sited_cases: set[int] = field(default_factory=set)
    _marker_targets: dict[int, _Symbol] = field(default_factory=dict)
    _marker_references: list[_MarkerReference] = field(default_factory=list)
    _constraint_references: list[_ConstraintReference] = field(default_factory=list)

    def parse(self, ast: ParsedProgram) -> IrConversionResult:
        for statement in ast.create_statements:
            match statement:
                case exp.Create(kind="INDEX"):
                    self._declare_index(statement)
                case exp.Create(kind="SCHEMA"):
                    pass  # A schema states nothing about rows, like a plain index.
                case exp.Create():
                    self._declare(statement)
                case exp.Alter():
                    self._alter(statement)
                case _:
                    raise TypeError(f"Unhandled schema statement: {type(statement).__name__}")

        for symbol in tuple(self._symbols):
            if symbol.role is ir.RelationRole.VIEW and symbol.body is not None:
                self._discover_query(symbol.body, {})
        if ast.root_select is not None:
            self._discover_query(ast.root_select, {})
        self._discover_constraint_references()
        self._propagate_required_columns()

        root = self._lower_query(ast.root_select) if ast.root_select is not None else None
        # Referenced relations are linked last: a marker on a view may name
        # the view itself, or one that reads it.
        self._link_constraint_references()
        self._link_marker_references()
        self._report_unanalyzed(ast)

        named_relations = tuple(self._named_relations.get(symbol) or self._unresolved_named_relation(symbol) for symbol in self._symbols)
        warnings = [*self._undeclared_table_warnings(), *self._warnings]
        return IrConversionResult(
            ir.Program(named_relations=named_relations, root=root, assertions=tuple(self._assertions), declarations=tuple(self._declarations), attachment_sites=tuple(self._attachment_sites)),
            tuple(self._knowledge), tuple(self._diagnostics), tuple(dict.fromkeys(warnings)),
        )

    def _undeclared_table_warnings(self) -> list[Diagnostic]:
        return [
            Diagnostic(
                diag.UNDECLARED_TABLE,
                f"table {symbol.name} has no CREATE TABLE in the program, so its keys, nullability and column types are unknown: "
                "declare it with CREATE TABLE, or analyze with a database connection (analyze_database)",
                symbol.origin,
            )
            for symbol in self._symbols
            if symbol.role is ir.RelationRole.TABLE and symbol.statement is None
        ]

    # Declaration and symbol discovery ---------------------------------------

    def _declare(self, statement: exp.Create) -> None:
        kind = (statement.kind or "").upper()
        if kind not in _SUPPORTED_CREATE_KINDS:
            self._diagnostics.append(
                Diagnostic(
                    diag.UNSUPPORTED_CREATE_STATEMENT,
                    f"create statement kind {kind or '<unknown>'} is not supported; only TABLE, VIEW, INDEX, and SCHEMA are analyzed: {statement.sql(dialect=self.dialect)}",
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

    def _declare_index(self, statement: exp.Create) -> None:
        """A unique index over plain columns is a Unique Set, like UNIQUE; any
        other index states nothing about the data."""
        index = statement.this
        table = index.args.get("table") if isinstance(index, exp.Index) else None
        if not isinstance(table, exp.Table):
            self._unsupported_create(statement, "an index without a table")
            return
        symbol = self._created_before(table, statement)
        if symbol is None or not statement.args.get("unique"):
            return
        params = index.args.get("params")
        columns = params.args.get("columns") or () if isinstance(params, exp.IndexParameters) else ()
        plain = [
            column.this for column in columns
            if isinstance(column, exp.Ordered) and isinstance(column.this, exp.Column) and not column.this.table
        ]
        if not columns or len(plain) != len(columns) or params.args.get("where") is not None:
            # Uniqueness of expressions, or of only some rows, is not a Unique Set.
            self._unsupported_create(statement, "a unique index that is partial or over expressions")
            return
        schema = exp.Schema(expressions=[exp.to_identifier(column.name) for column in plain])
        symbol.constraints.append(exp.UniqueColumnConstraint(this=schema))

    def _alter(self, statement: exp.Alter) -> None:
        """ALTER TABLE adding PRIMARY KEY, UNIQUE, or FOREIGN KEY constraints, or
        setting columns NOT NULL. Any other action could invalidate what is
        known of the table, so a statement with one states nothing."""
        actions = statement.args.get("actions") or ()
        added: list[exp.Expression] = []
        not_null: list[str] = []
        for action in actions:
            match action:
                case exp.AddConstraint():
                    added.extend(_unwrap_constraints(action.expressions))
                case exp.AlterColumn() if action.args.get("allow_null") is False and {
                    key for key, value in action.args.items() if value is not None and value is not False
                } == {"this"}:
                    not_null.append(action.name)
                case _:
                    added.append(action)
        if not actions or not all(_is_added_constraint(constraint) for constraint in added):
            self._diagnostics.append(Diagnostic(
                diag.UNSUPPORTED_STATEMENT,
                f"only ALTER TABLE adding PRIMARY KEY, UNIQUE, or FOREIGN KEY constraints, or setting NOT NULL, "
                f"is analyzed: {statement.sql(dialect=self.dialect)}",
                self._origin(statement),
            ))
            return
        table = statement.this
        symbol = self._created_before(table, statement) if isinstance(table, exp.Table) else None
        if symbol is None:
            return
        symbol.constraints.extend(added)
        symbol.not_null.update(not_null)

    def _created_before(self, table: exp.Table, statement: exp.Expression) -> _Symbol | None:
        """The table `statement` amends, which a CREATE TABLE earlier in the
        program must declare; otherwise the statement is diagnosed."""
        name = _qualified_name(table)
        symbol = self._global_symbols.get(name.lower())
        if symbol is None or symbol.role is not ir.RelationRole.TABLE or symbol.statement is None:
            self._diagnostics.append(Diagnostic(
                diag.CONSTRAINT_WITHOUT_TABLE,
                f"{name} is not a table created earlier in the program, so this statement's constraints are ignored: "
                f"{statement.sql(dialect=self.dialect)}",
                self._origin(statement),
            ))
            return None
        return symbol

    def _unsupported_create(self, statement: exp.Create, what: str) -> None:
        self._diagnostics.append(Diagnostic(
            diag.UNSUPPORTED_CREATE_STATEMENT,
            f"{what} is not analyzed: {statement.sql(dialect=self.dialect)}",
            self._origin(statement),
        ))

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
            for marker in column_markers(query):
                for column in (*marker.columns, *marker.dependent):
                    self._require(sources[0], column)

        for marker in column_markers(query):
            if marker.referenced_relation is not None:
                origin = Origin(SQL, marker.referenced_relation, marker.line)
                target = self._symbol_for_name(marker.referenced_relation, scope, origin)
                self._marker_targets[id(marker)] = target
                self._require_undeclared(target, marker.referenced_columns)

        where = query.args.get("where")
        for conjunct in _conjuncts(where.this) if where is not None else ():
            matched = _subquery_conjunct(conjunct)
            if matched is not None:
                self._discover_query(matched[1], scope)

        if with_clause is not None:
            for cte in with_clause.expressions:
                body = _unwrap_query(cte.this)
                if body is not None:
                    self._discover_query(body, scope)

    def _symbol_for_table(self, table: exp.Table, scope: dict[str, _Symbol]) -> _Symbol:
        return self._symbol_for_name(_qualified_name(table), scope, Origin(SQL, table.sql(dialect=self.dialect)))

    def _symbol_for_name(self, name: str, scope: dict[str, _Symbol], origin: Origin) -> _Symbol:
        key = name.lower()
        if key in scope:
            return scope[key]
        symbol = self._global_symbols.get(key)
        if symbol is None:
            symbol = _Symbol(name=name, role=ir.RelationRole.TABLE, origin=origin)
            self._global_symbols[key] = symbol
            self._symbols.append(symbol)
        return symbol

    def _require_undeclared(self, symbol: _Symbol, columns: Iterable[str]) -> None:
        """Referenced columns become columns of an undeclared table only: a
        declared relation's schema is never widened by a reference to it."""
        if symbol.role is ir.RelationRole.TABLE and symbol.statement is None:
            symbol.required_columns.update(columns)

    def _discover_constraint_references(self) -> None:
        """`REFERENCES` constraints onto relations the program reaches; one onto
        a relation outside the program can never be used, so it is not linked."""
        for symbol in tuple(self._symbols):
            if symbol.role is not ir.RelationRole.TABLE or symbol.statement is None:
                continue
            for columns, table, target_columns, node in _references(symbol):
                target = self._global_symbols.get(_qualified_name(table).lower())
                if target is None or target.role is not ir.RelationRole.TABLE:
                    continue
                self._require_undeclared(target, target_columns or ())
                self._constraint_references.append(
                    _ConstraintReference(symbol, columns, target, target_columns, self._origin(node))
                )

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

    def _lower_query(self, query: exp.Query) -> ir.RelationExpr | None:
        """A Root Select, view, CTE, and FROM-subquery body all lower the same way."""
        unwrapped = _unwrap_query(query)
        if unwrapped is None:
            return None
        query = unwrapped
        if isinstance(query, exp.SetOperation):
            left = self._lower_query(query.this)
            right = self._lower_query(query.expression)
            if left is None:
                left = self._opaque_relation(query.this)
            if right is None:
                right = self._opaque_relation(query.expression)
            output_columns = _pass_output_columns(left.output_columns, self._origin(query))
            relation = ir.SetOperation(
                origin=self._origin(query), output_columns=output_columns, is_schema_complete=left.is_schema_complete and right.is_schema_complete,
                operator=_SET_OPERATORS[type(query)], left=left, right=right, distinct=bool(query.args.get("distinct")),
                by_name=bool(query.args.get("by_name")),
            )
            return self._lower_limit(relation, query)
        if not isinstance(query, exp.Select):
            return None
        return self._lower_select(query)

    def _lower_select(self, select: exp.Select) -> ir.RelationExpr | None:
        source = _from_source(select)
        if source is None:
            # No FROM: the select list is evaluated once, over one row of no
            # columns, which an opaque relation limited to one row stands for.
            origin = self._origin(select)
            row = ir.OpaqueRelation(origin=origin, output_columns=(), is_schema_complete=True, description="no FROM")
            one_row = ir.Limit(origin=origin, output_columns=(), is_schema_complete=True, input=row, count=1)
            relation = self._lower_select_tail(_Lowered(one_row, ()), select)
        elif any(join.args.get("method") == "NATURAL" for join in select.args.get("joins") or ()):
            relation = self._opaque_relation(select, _selected_output_names(select))
        else:
            lowered = self._lower_source(source)
            for join in select.args.get("joins") or ():
                lowered = self._lower_join(lowered, join)
            relation = self._lower_select_tail(lowered, select)
        relation = self._lower_limit(relation, select)
        self._register_column_markers(relation, select)
        self._add_attachment_site(select, _select_label(select), relation, trailing=True)
        return relation

    def _lower_limit(self, relation: ir.RelationExpr, query: exp.Query) -> ir.RelationExpr:
        limit = query.args.get("limit")
        if limit is None and query.args.get("offset") is None:
            return relation
        count = None
        if limit is not None:
            expression = limit.args.get("expression") if isinstance(limit, exp.Limit) else limit.args.get("count")
            options = limit.args.get("limit_options")
            hard_bound = options is None or not any(options.args.get(key) for key in ("percent", "with_ties"))
            if hard_bound and isinstance(expression, exp.Literal) and expression.is_int:
                count = int(expression.this)
        origin = self._origin(query)
        return ir.Limit(origin=origin, output_columns=_pass_output_columns(relation.output_columns, origin), is_schema_complete=relation.is_schema_complete, input=relation, count=count)

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

        if isinstance(source, exp.Subquery):
            relation = self._lower_query(source)
            if relation is not None:
                # DuckDB allows an unaliased FROM subquery; nothing can qualify
                # a column by its name, so a placeholder serves.
                return self._alias(relation, source.alias or "subquery", self._origin(source))

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
            self._knowledge.extend(self._table_knowledge(named, symbol))
            return named

        self._resolving.add(symbol)
        try:
            body = self._lower_query(symbol.body) if symbol.body is not None else None
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
            self._knowledge.extend(self._table_knowledge(named, symbol))
        return named

    def _table_output_columns(self, symbol: _Symbol) -> tuple[tuple[ir.OutputColumn, ...], bool]:
        definitions = list(symbol.statement.this.find_all(exp.ColumnDef)) if symbol.statement is not None else []
        names = [definition.name for definition in definitions]
        seen = {name.lower() for name in names}
        names.extend(column for column in sorted(symbol.required_columns, key=str.lower) if column.lower() not in seen)
        complete = symbol.statement is not None and {column.lower() for column in symbol.required_columns} <= seen
        columns = self._opaque_output_columns(names, symbol.origin)
        by_name = {definition.name: definition for definition in definitions}
        return tuple(
            replace(
                column, binary_text=self._is_binary_text(by_name[column.name]),
                comparison_type=self._comparison_type(by_name[column.name]),
                identity_comparison=self._identity_comparison(by_name[column.name]),
                exact_numeric=self._exact_numeric(by_name[column.name]),
            )
            if column.name in by_name else column for column in columns
        ), complete

    def _comparison_type(self, definition: exp.ColumnDef) -> str | None:
        kind = definition.args.get("kind")
        if not isinstance(kind, exp.DataType):
            return None
        if any(isinstance(constraint.args.get("kind"), exp.CollateColumnConstraint) for constraint in definition.constraints):
            return None
        if kind.this in exp.DataType.TEXT_TYPES and not self._is_binary_text(definition):
            return None
        return kind.sql(dialect=self.dialect)

    def _identity_comparison(self, definition: exp.ColumnDef) -> bool:
        """Declared with a type whose equal values are identical (text only
        when compared by identity, which `_comparison_type` already checks)."""
        kind = definition.args.get("kind")
        return self._comparison_type(definition) is not None and isinstance(kind, exp.DataType) and kind.this in _IDENTITY_TYPES

    def _exact_numeric(self, definition: exp.ColumnDef) -> bool:
        kind = definition.args.get("kind")
        return isinstance(kind, exp.DataType) and kind.this in _EXACT_NUMERIC_TYPES

    def _is_binary_text(self, definition: exp.ColumnDef) -> bool:
        """Declared VARCHAR or TEXT under the dialect's binary default collation.

        CHAR is not: it ignores trailing spaces when compared."""
        kind = definition.args.get("kind")
        return (
            self.dialect in _BINARY_COLLATION_DIALECTS
            and isinstance(kind, exp.DataType) and kind.this in _TEXT_TYPES
            and not any(isinstance(constraint.args.get("kind"), exp.CollateColumnConstraint) for constraint in definition.constraints)
        )

    def _opaque_output_columns(self, names: Iterable[str], origin: Origin) -> tuple[ir.OutputColumn, ...]:
        if isinstance(names, Set):
            names = sorted(names, key=lambda name: (name.lower(), name))
        return tuple(
            ir.OutputColumn(
                origin=origin,
                name=name,
                scalar_expr=ir.OpaqueExpression(origin=origin, description=f"source column {name}"),
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
        predicate = self._lower_join_predicate(join, left, right)
        if _join_kind(join) in {"semi", "anti"}:
            # RIGHT SEMI and RIGHT ANTI keep right rows: the same join, inputs swapped.
            kept, other = (right, left) if (join.args.get("side") or "").lower() == "right" else (left, right)
            filtered = self._filtering_join(kept, other.relation, _join_kind(join), predicate, origin)
            assert isinstance(filtered.relation, ir.Join)
            self._join_marker(join, filtered.relation)
            self._add_attachment_site(join, _join_label(join, self.dialect), filtered.relation, trailing=False)
            return filtered
        if _is_positional(join):
            # A FULL JOIN on row position: an opaque condition, as no column holds it.
            predicate = ir.OpaqueExpression(origin=origin, description="row position")
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
            predicate=predicate,
        )
        bindings = tuple(
            _Binding(binding.qualifier, binding.name, remapped[binding.column])
            for binding in left.bindings + right.bindings
        )
        self._join_marker(join, relation)
        self._add_attachment_site(join, _join_label(join, self.dialect), relation, trailing=False)
        return _Lowered(relation, bindings)

    def _join_marker(self, join: exp.Join, relation: ir.Join) -> None:
        if join_marker_kind(join) is not None:
            self._handled_joins.add(id(join))
        match join_marker_kind(join):
            case "unique-join":
                self._assertions.append(ir.Assertion(origin=join_origin(join, self.dialect), property=UniqueJoin(join=relation)))
            case "exactly-one-join":
                self._assertions.append(
                    ir.Assertion(origin=join_origin(join, self.dialect), property=ExactlyOneJoin(join=relation))
                )
            case None:
                pass
            case kind:
                raise ValueError(f"Unhandled join marker kind: {kind}")

    def _lower_join_predicate(self, join: exp.Join, left: _Lowered, right: _Lowered) -> ir.ScalarExpr | None:
        """The ON condition; USING is an AND of one equality per column."""
        using = join.args.get("using")
        if using:
            return _and(tuple(
                ir.Equality(
                    origin=self._origin(identifier, _source_line(identifier)),
                    left=self._lower_expression(exp.column(identifier.name), left.bindings),
                    right=self._lower_expression(exp.column(identifier.name), right.bindings),
                )
                for identifier in using
            ), self._origin(join))
        predicate = join.args.get("on")
        if predicate is None:
            return None
        complete = left.relation.is_schema_complete and right.relation.is_schema_complete
        return self._lower_condition(predicate, _conjuncts(predicate), left.bindings + right.bindings, schema_complete=complete)

    def _lower_condition(
        self, source: exp.Expression, conjuncts: list[exp.Expression], bindings: tuple[_Binding, ...], *, schema_complete: bool
    ) -> ir.ScalarExpr:
        """The AND of `conjuncts`, the top-level conjuncts of `source`."""
        assert conjuncts, "a condition has at least one conjunct"
        lowered = _and(
            tuple(self._lower_conjunct(conjunct, bindings, schema_complete=schema_complete) for conjunct in conjuncts),
            self._origin(source, _source_line(source)),
        )
        assert lowered is not None
        return lowered

    def _lower_conjunct(self, conjunct: exp.Expression, bindings: tuple[_Binding, ...], *, schema_complete: bool) -> ir.ScalarExpr:
        return self._lower_expression(conjunct, bindings, schema_complete=schema_complete, line=_source_line(conjunct))

    def _lower_select_tail(self, lowered: _Lowered, select: exp.Select) -> ir.RelationExpr:
        group = select.args.get("group")
        if isinstance(group, exp.Group):
            relation = self._lower_aggregate(lowered, select, group)
        elif _is_scalar_aggregate(select):
            # One row, however many rows its input has: grouped by nothing.
            relation = self._lower_aggregate(lowered, select, exp.Group(expressions=[]))
        elif select.args.get("distinct") is not None and select.args["distinct"].args.get("on") is not None:
            relation = self._lower_distinct_on(lowered, select, select.args["distinct"].args["on"])
        elif select.args.get("distinct") is not None:
            relation = self._lower_distinct(lowered, select)
        elif isinstance(select.args.get("qualify"), exp.Qualify):
            relation = self._lower_qualify(lowered, select, select.args["qualify"])
        else:
            if select.args.get("where") is not None:
                lowered = self._filter(lowered, select.args["where"])
            if _star_only(select):
                relation = lowered.relation
            else:
                relation = self._project(lowered, select.expressions, self._origin(select))
        return relation if relation is not None else self._opaque_relation(select, _selected_output_names(select))

    def _filter(self, lowered: _Lowered, where: exp.Where) -> _Lowered:
        """WHERE: each `EXISTS`, `NOT EXISTS`, or `IN (subquery)` conjunct as a
        semi or anti join (Paulley §4.4.2), the other conjuncts as one Filter."""
        remaining = []
        for conjunct in _conjuncts(where.this):
            joined = self._subquery_join(lowered, conjunct)
            if joined is None:
                remaining.append(conjunct)
            else:
                lowered = joined
        if not remaining:
            return lowered
        origin = self._origin(where)
        output_columns = _pass_output_columns(lowered.relation.output_columns, origin)
        remapped = {source: output for source, output in zip(lowered.relation.output_columns, output_columns)}
        complete = lowered.relation.is_schema_complete
        relation = ir.Filter(
            origin=origin, output_columns=output_columns, is_schema_complete=complete, input=lowered.relation,
            predicate=self._lower_condition(where.this, remaining, lowered.bindings, schema_complete=complete),
        )
        bindings = tuple(
            _Binding(binding.qualifier, binding.name, remapped[binding.column]) for binding in lowered.bindings
        )
        return _Lowered(relation, bindings)

    def _subquery_join(self, lowered: _Lowered, conjunct: exp.Expression) -> _Lowered | None:
        """`EXISTS (SELECT ... FROM r WHERE p)` as a semi join of r on p, and
        `NOT EXISTS` as an anti join; `p` may read the outer row (a dependent
        join, so the IR stays a DAG). `x IN (SELECT a ...)` as a semi join on
        `x = a`. Not `NOT IN`: a NULL among the values makes it reject every row."""
        matched = _subquery_conjunct(conjunct)
        if matched is None:
            return None
        kind, select, operand = matched
        if operand is None:
            source = _from_source(select)
            if source is None or any(
                select.args.get(key) for key in ("group", "having", "qualify", "distinct", "limit", "offset", "with", "windows")
            ) or any(join.args.get("method") == "NATURAL" for join in select.args.get("joins") or ()):
                return None
            right = self._lower_source(source)
            for join in select.args.get("joins") or ():
                right = self._lower_join(right, join)
            inner = select.args.get("where")
            predicate = None if inner is None else self._lower_condition(
                inner.this, _conjuncts(inner.this), lowered.bindings + right.bindings,
                schema_complete=lowered.relation.is_schema_complete and right.relation.is_schema_complete,
            )
            relation = right.relation
        else:
            subquery = self._lower_query(select)
            if subquery is None or len(subquery.output_columns) != 1:
                return None
            relation = subquery
            predicate = ir.Equality(
                origin=self._origin(conjunct, _source_line(conjunct)),
                left=self._lower_expression(operand, lowered.bindings, schema_complete=lowered.relation.is_schema_complete),
                right=ir.ColumnRef(origin=subquery.origin, column=subquery.output_columns[0]),
            )
        return self._filtering_join(lowered, relation, kind, predicate, self._origin(conjunct))

    def _filtering_join(
        self, left: _Lowered, right: ir.RelationExpr, kind: ir.JoinKind, predicate: ir.ScalarExpr | None, origin: Origin
    ) -> _Lowered:
        """A semi or anti join: it keeps or drops left rows, and outputs only theirs."""
        output_columns = _pass_output_columns(left.relation.output_columns, origin)
        remapped = {source: output for source, output in zip(left.relation.output_columns, output_columns)}
        relation = ir.Join(
            origin=origin, output_columns=output_columns, is_schema_complete=left.relation.is_schema_complete,
            kind=kind, left=left.relation, right=right, predicate=predicate,
        )
        return _Lowered(relation, tuple(_Binding(binding.qualifier, binding.name, remapped[binding.column]) for binding in left.bindings))

    def _project(self, lowered: _Lowered, items: list[exp.Expression], origin: Origin) -> ir.Project:
        output_columns = tuple(
            ir.OutputColumn(
                origin=self._origin(item),
                name=item.alias_or_name,
                scalar_expr=self._lower_expression(
                    item.this if isinstance(item, exp.Alias) else item,
                    lowered.bindings,
                    schema_complete=lowered.relation.is_schema_complete,
                ),
            )
            for item in items
        )
        return ir.Project(origin=origin, output_columns=output_columns, is_schema_complete=True, input=lowered.relation)

    def _lower_aggregate(self, lowered: _Lowered, select: exp.Select, group: exp.Group) -> ir.Aggregate | None:
        if select.args.get("having") is not None or any(group.args.get(key) for key in ("grouping_sets", "rollup", "cube", "totals")):
            return None
        if select.args.get("where") is not None:
            lowered = self._filter(lowered, select.args["where"])

        grouped_items: set[int] = set()
        for group_expression in group.expressions:
            item = _matching_grouping_output(group_expression, select.expressions)
            if item is None:
                return None
            grouped_items.add(id(item))

        output_columns = self._aggregate_output_columns(select.expressions, lowered, grouped_items)
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
        if distinct is None or select.args.get("having") is not None or _star_only(select):
            return None
        if select.args.get("where") is not None:
            lowered = self._filter(lowered, select.args["where"])
        return ir.Distinct(
            origin=self._origin(select), output_columns=self._selected_output_columns(select.expressions, lowered),
            is_schema_complete=True, input=lowered.relation,
        )

    def _lower_qualify(self, lowered: _Lowered, select: exp.Select, qualify: exp.Qualify) -> ir.QualifyByPartition | None:
        window = _row_number_equals_one(qualify.this)
        if window is None:
            return None
        order = window.args.get("order")
        return self._one_row_per_partition(
            lowered, select, window.args.get("partition_by") or (), order.expressions if order else (),
        )

    def _lower_distinct_on(self, lowered: _Lowered, select: exp.Select, on: exp.Expression) -> ir.QualifyByPartition | None:
        """`DISTINCT ON (k...)` keeps one row per distinct `k...`, like
        `QUALIFY ROW_NUMBER() OVER (PARTITION BY k...) = 1`. Its ORDER BY only
        picks which row survives, and may name outputs, so it is not lowered."""
        if select.args.get("qualify") is not None:
            return None
        keys = on.expressions if isinstance(on, exp.Tuple) else [on]
        return self._one_row_per_partition(lowered, select, keys, ())

    def _one_row_per_partition(
        self, lowered: _Lowered, select: exp.Select, partitions: Sequence[exp.Expression], order: Sequence[exp.Ordered],
    ) -> ir.QualifyByPartition | None:
        """Every partition key must be a selected output: the Unique Set is stated on outputs."""
        if not partitions or select.args.get("having") is not None or _star_only(select):
            return None
        if select.args.get("where") is not None:
            lowered = self._filter(lowered, select.args["where"])

        output_columns = self._selected_output_columns(select.expressions, lowered)
        partition_outputs: list[ir.OutputColumn] = []
        for partition in partitions:
            item = _matching_output(partition, select.expressions)
            if item is None:
                return None
            partition_outputs.append(output_columns[select.expressions.index(item)])
        ordering = tuple(
            self._lower_expression(ordered.this, lowered.bindings, schema_complete=lowered.relation.is_schema_complete)
            for ordered in order
        )
        return ir.QualifyByPartition(
            origin=self._origin(select), output_columns=output_columns, is_schema_complete=True, input=lowered.relation,
            partition_outputs=tuple(partition_outputs), ordering=ordering,
        )

    def _selected_output_columns(self, items: list[exp.Expression], lowered: _Lowered) -> tuple[ir.OutputColumn, ...]:
        return tuple(
            ir.OutputColumn(
                origin=self._origin(item),
                name=item.alias_or_name,
                scalar_expr=self._lower_expression(
                    item.this if isinstance(item, exp.Alias) else item,
                    lowered.bindings,
                    schema_complete=lowered.relation.is_schema_complete,
                ),
            )
            for item in items
        )

    def _aggregate_output_columns(self, items: list[exp.Expression], source: _Lowered, grouped_items: set[int]) -> tuple[ir.OutputColumn, ...]:
        output_columns: list[ir.OutputColumn] = []
        for item in items:
            expression = item.this if isinstance(item, exp.Alias) else item
            lowered = self._lower_expression(expression, source.bindings, schema_complete=source.relation.is_schema_complete)
            if id(item) not in grouped_items and _is_bare_aggregate_expression(expression):
                # Grouped by nothing (no grouped items), the one row exists even
                # over no input rows, so a bare column, where a dialect allows
                # one, may be NULL.
                lowered = (
                    ir.AnyAggregate(origin=self._origin(expression), input=lowered) if grouped_items
                    else ir.OpaqueExpression(origin=self._origin(expression), description=expression.sql(dialect=self.dialect))
                )
            output_columns.append(ir.OutputColumn(origin=self._origin(item), name=item.alias_or_name, scalar_expr=lowered))
        return tuple(output_columns)

    def _lower_expression(
        self, expression: exp.Expression, bindings: tuple[_Binding, ...], *, schema_complete: bool = False, line: int | None = None
    ) -> ir.ScalarExpr:
        """An unqualified column resolves by name across several sources only
        when every source's schema is complete: otherwise the one visible match
        might be shadowed by a column of a source whose columns are unknown.

        A Builtin becomes a Call; any other function or operator an
        OpaquePureFunction of its lowered arguments, unless its value is not a
        function of them (`_NOT_A_FUNCTION_OF_ITS_COLUMNS`)."""
        origin = self._origin(expression, line)

        def lower(node: exp.Expression) -> ir.ScalarExpr:
            return self._lower_expression(node, bindings, schema_complete=schema_complete)

        if isinstance(expression, exp.Column):
            if expression.table:
                matches = [binding for binding in bindings if binding.qualifier == expression.table.lower() and binding.name == expression.name.lower()]
            elif schema_complete or len({binding.qualifier for binding in bindings}) == 1:
                matches = [binding for binding in bindings if binding.name == expression.name.lower()]
            else:
                matches = []
                sources = ", ".join(dict.fromkeys(binding.qualifier for binding in bindings))
                self._warnings.append(Diagnostic(
                    diag.AMBIGUOUS_COLUMN,
                    f"column {expression.name} is unqualified and more than one source may own it ({sources}), "
                    "since some have no declared columns: qualify it, declare those tables with CREATE TABLE, "
                    "or analyze with a database connection (analyze_database)",
                    origin,
                ))
            if len(matches) == 1:
                return ir.ColumnRef(origin=origin, column=matches[0].column)
            return ir.OpaqueExpression(origin=origin, description=expression.sql(dialect=self.dialect))
        description = expression.sql(dialect=self.dialect)
        quantified = isinstance(expression, exp.Binary) and any(
            isinstance(operand, (exp.Any, exp.All)) for operand in (expression.this, expression.expression)
        )  # `n > ANY (...)` is FALSE over no rows, even for a NULL `n`.
        match expression:
            case exp.Paren():
                return self._lower_expression(expression.this, bindings, schema_complete=schema_complete, line=line)
            case exp.Null():
                return ir.NullLiteral(origin=origin)
            case exp.Count():
                return ir.Count(origin=origin, description=description)
            case exp.Coalesce():
                return ir.Coalesce(origin=origin, arguments=tuple(lower(argument) for argument in (expression.this, *expression.expressions)))
            case exp.Case() if expression.this is not None:
                return self._lower_case(expression, bindings, schema_complete=schema_complete)
            case exp.And():
                return self._lower_condition(expression, _conjuncts(expression), bindings, schema_complete=schema_complete)
            case exp.Or():
                return ir.Or(origin=origin, parts=tuple(
                    self._lower_condition(branch, _conjuncts(branch), bindings, schema_complete=schema_complete)
                    for branch in _disjuncts(expression)
                ))
            case exp.EQ() if not quantified:
                return ir.Equality(origin=origin, left=lower(expression.this), right=lower(expression.expression))
            case exp.Is(expression=exp.Null()):
                return ir.IsNull(origin=origin, operand=lower(expression.this))
            case exp.Not(this=exp.Is(expression=exp.Null()) as test):
                return ir.IsNotNull(origin=origin, operand=lower(test.this))
            case exp.In() if expression.expressions and not any(expression.args.get(key) for key in ("query", "unnest", "field")):
                return ir.InList(origin=origin, operand=lower(expression.this), values=tuple(lower(value) for value in expression.expressions))
            case _:
                pass
        value = literal_value(expression)
        if value is not None or isinstance(expression, exp.Literal):
            return ir.Constant(origin=origin, value=value)
        builtin = _builtins(self.dialect).get(type(expression))
        if builtin is not None and not quantified:
            function, keys = builtin
            values = [value for key in keys for value in _as_list(expression.args[key])]
            if function.accepts(len(values)):
                return ir.Call(origin=origin, function=function, arguments=tuple(lower(value) for value in values), description=description)
        if isinstance(expression, _NOT_A_FUNCTION_OF_ITS_COLUMNS):
            return ir.OpaqueExpression(origin=origin, description=description)
        return ir.OpaquePureFunction(origin=origin, arguments=tuple(lower(child) for child in expression.iter_expressions()), description=description)

    def _lower_case(self, case: exp.Case, bindings: tuple[_Binding, ...], *, schema_complete: bool) -> ir.SimpleCase:
        """A simple CASE, and the assertion its marker states, once per CASE."""

        def lower(expression: exp.Expression) -> ir.ScalarExpr:
            return self._lower_expression(expression, bindings, schema_complete=schema_complete)

        arms = case.args.get("ifs") or ()
        default = case.args.get("default")
        lowered = ir.SimpleCase(
            origin=self._origin(case),
            operand=lower(case.this),
            whens=tuple(lower(arm.this) for arm in arms),
            thens=tuple(lower(arm.args["true"]) for arm in arms),
            default=lower(default) if default is not None else ir.NullLiteral(origin=self._origin(case)),
        )
        if id(case) not in self._sited_cases:
            self._sited_cases.add(id(case))
            self._add_attachment_site(case, f"CASE {case.this.sql(dialect=self.dialect)}", lowered, trailing=False)
        line = exhaustive_line(case)
        if line is not None and id(case) not in self._handled_cases:
            self._handled_cases.add(id(case))
            origin = Origin(SQL, case.sql(dialect=self.dialect), line)
            self._assertions.append(ir.Assertion(origin=origin, property=ExhaustiveCase(case=lowered)))
        return lowered

    def _opaque_relation(self, source: exp.Expression, names=()) -> ir.OpaqueRelation:
        origin = self._origin(source)
        return ir.OpaqueRelation(
            origin=origin,
            output_columns=self._opaque_output_columns(names, origin),
            is_schema_complete=False,
            description=source.sql(dialect=self.dialect),
        )

    # Assertions and diagnostics --------------------------------------------

    def _register_column_markers(self, relation: ir.RelationExpr, select: exp.Select) -> None:
        by_name = {output.name.lower(): output for output in relation.output_columns}
        for marker in column_markers(select):
            kind, columns, line = marker.kind, marker.columns, marker.line
            self._handled_column_markers.add(id(marker))
            origin = Origin(SQL, select.sql(dialect=self.dialect), line)
            unknown = [column for column in (*columns, *marker.dependent) if column.lower() not in by_name]
            if unknown and relation.is_schema_complete:
                self._diagnostics.append(
                    Diagnostic(
                        diag.UNKNOWN_ASSERTED_COLUMN,
                        f"the {MARKER_NOUNS[kind]} on line {line} names {', '.join(unknown)}, which is not among this Select Expression's own output columns",
                        origin,
                    )
                )
                continue
            if unknown:
                # The column may exist in a source whose columns are unknown,
                # so it is neither absent nor something to state a Property of.
                self._diagnostics.append(
                    Diagnostic(
                        diag.UNRESOLVED_ASSERTED_COLUMN,
                        f"the {MARKER_NOUNS[kind]} on line {line} names {', '.join(unknown)}, which cannot be resolved: "
                        "this Select Expression reads a relation whose columns are unknown (declare it with CREATE TABLE)",
                        origin,
                    )
                )
                continue
            if kind in FOREIGN_KEY_KINDS:
                self._marker_references.append(_MarkerReference(
                    marker, origin, tuple(by_name[column.lower()] for column in columns),
                    self._marker_targets[id(marker)], len(self._assertions),
                ))
                continue
            members = frozenset(by_name[column.lower()] for column in columns)
            dependent = frozenset(by_name[column.lower()] for column in marker.dependent)
            match kind:
                case "unique":
                    self._assertions.append(ir.Assertion(origin=origin, property=UniqueSet(columns=members)))
                case "key":
                    self._assertions.append(ir.Assertion(origin=origin, property=NonNullUniqueSet(columns=members)))
                case "not-null":
                    self._assertions.append(ir.Assertion(origin=origin, property=NonNullColumns(columns=members)))
                case "declared-unique":
                    self._declarations.append(UniqueSet(columns=members))
                case "declared-key":
                    # Non-nullness is declared per column too, so it outlives
                    # projections that drop part of a composite key.
                    self._declarations.append(NonNullUniqueSet(columns=members))
                    self._declarations.extend(NonNullColumn(column=column) for column in members)
                case "declared-not-null":
                    self._declarations.extend(NonNullColumn(column=column) for column in members)
                case "determines":
                    property = FunctionalDependency(determinant=members, dependent=dependent)
                    self._assertions.append(ir.Assertion(origin=origin, property=property))
                case "declared-determines":
                    self._declarations.append(FunctionalDependency(determinant=members, dependent=dependent))
                case "enum" | "declared-enum":
                    column, = members
                    values = frozenset(marker.values)
                    domain = EnumeratedDomain(column=column, values=values)
                    if kind == "enum":
                        self._assertions.append(ir.Assertion(origin=origin, property=domain))
                    else:
                        self._declarations.append(domain)
                case _:
                    raise ValueError(f"Unhandled column marker kind: {kind}")

    def _add_attachment_site(
        self, node: exp.Join | exp.Select | exp.Case, label: str, relation: ir.RelationExpr | ir.SimpleCase, *, trailing: bool
    ) -> None:
        position = marker_position(node)
        if position is not None:
            offset, line = position
            self._attachment_sites.append((AttachmentSite(label, line, offset, trailing), relation))

    def _link_marker_references(self) -> None:
        """Lower each Foreign Key marker's referenced relation, then state the
        marker's Property; assertions keep the place their marker was written."""
        placed: list[tuple[int, ir.Assertion]] = []
        while self._marker_references:
            pending = self._marker_references.pop(0)
            target = self._resolve_symbol(pending.target)
            marker = pending.marker
            referenced = self._columns_named(target, marker.referenced_columns)
            if referenced is None:
                self._diagnostics.append(Diagnostic(
                    diag.UNKNOWN_REFERENCED_COLUMN,
                    f"the {MARKER_NOUNS[marker.kind]} on line {marker.line} references "
                    f"{target.name}({', '.join(marker.referenced_columns)}), but {target.name} has no column "
                    f"{', '.join(_missing(target, marker.referenced_columns))}",
                    pending.origin,
                ))
                continue
            match marker.kind:
                case "foreign-key":
                    placed.append((pending.position, ir.Assertion(
                        origin=pending.origin, property=ForeignKey(columns=pending.columns, referenced_columns=referenced),
                    )))
                case "declared-foreign-key":
                    self._declarations.append(ReferentialCoverage(columns=pending.columns, referenced_columns=referenced))
                case _:
                    raise ValueError(f"Unhandled foreign key marker kind: {marker.kind}")
        # Descending, so earlier positions stay valid; ties keep written order.
        for position, assertion in reversed(placed):
            self._assertions.insert(position, assertion)

    def _link_constraint_references(self) -> None:
        for reference in self._constraint_references:
            table = self._resolve_symbol(reference.table)
            target = self._resolve_symbol(reference.target)
            target_names = reference.target_columns
            if target_names is None:
                if reference.target.statement is None:
                    continue  # An undeclared table's primary key is unknown.
                target_names = _primary_key(reference.target)
                if target_names is None:
                    self._diagnostics.append(Diagnostic(
                        diag.UNKNOWN_REFERENCED_COLUMN,
                        f"a foreign key of {table.name} references {target.name} without columns, "
                        f"and {target.name} declares no primary key",
                        reference.origin,
                    ))
                    continue
            columns = self._columns_named(table, reference.columns)
            if columns is None or len(reference.columns) != len(target_names):
                self._diagnostics.append(Diagnostic(
                    diag.INVALID_FOREIGN_KEY_CONSTRAINT,
                    f"a foreign key of {table.name} must pair columns of its own with equally many columns of "
                    f"{target.name}: ({', '.join(reference.columns)}) references ({', '.join(target_names)})",
                    reference.origin,
                ))
                continue
            referenced = self._columns_named(target, target_names)
            if referenced is None:
                self._diagnostics.append(Diagnostic(
                    diag.UNKNOWN_REFERENCED_COLUMN,
                    f"a foreign key of {table.name} references {target.name}({', '.join(target_names)}), "
                    f"but {target.name} has no column {', '.join(_missing(target, target_names))}",
                    reference.origin,
                ))
                continue
            self._knowledge.append(ReferentialCoverage(columns=columns, referenced_columns=referenced))

    def _columns_named(self, relation: ir.RelationExpr, names: Iterable[str]) -> tuple[ir.OutputColumn, ...] | None:
        by_name = {column.name.lower(): column for column in relation.output_columns}
        columns = [by_name.get(name.lower()) for name in names]
        found = tuple(column for column in columns if column is not None)
        return found if len(found) == len(columns) else None

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
        for join in _asserted_joins(ast):
            line, kind = assertion_line(join), join_marker_kind(join)
            if id(join) in self._handled_joins:
                continue
            assert kind is not None, "every asserted join records its marker kind"
            self._diagnostics.append(
                Diagnostic(
                    diag.UNANALYZED_ASSERTION,
                    f"the {JOIN_MARKER_NOUNS[kind]} assertion on line {line} "
                    "is in a part of the program this analysis does not model",
                    join_origin(join, self.dialect),
                )
            )
        for case in _asserted_cases(ast):
            if id(case) in self._handled_cases:
                continue
            line = exhaustive_line(case)
            self._diagnostics.append(
                Diagnostic(
                    diag.UNANALYZED_ASSERTION,
                    f"the exhaustive assertion on line {line} is in a part of the program this analysis does not model",
                    Origin(SQL, case.sql(dialect=self.dialect), line),
                )
            )
        for select in _asserted_selects(ast):
            for marker in column_markers(select):
                kind, line = marker.kind, marker.line
                if id(marker) in self._handled_column_markers:
                    continue
                self._diagnostics.append(
                    Diagnostic(
                        diag.UNANALYZED_ASSERTION,
                        f"the {MARKER_NOUNS[kind]} on line {line} is in a part of the program this analysis does not model",
                        Origin(SQL, select.sql(dialect=self.dialect), line),
                    )
                )

    def _table_knowledge(self, relation: ir.NamedRelation, symbol: _Symbol) -> tuple[Property, ...]:
        statement = symbol.statement
        if statement is None:
            return ()
        columns = {column.name.lower(): column for column in relation.output_columns}
        facts: list[Property] = []
        unique_sets: list[tuple[str, ...]] = []
        non_null = {column.lower() for column in symbol.not_null}
        for definition in statement.this.find_all(exp.ColumnDef):
            domain = _enum_type_values(definition)
            if domain is not None and definition.name.lower() in columns:
                facts.append(EnumeratedDomain(column=columns[definition.name.lower()], values=domain))
            kinds = [type(constraint.args.get("kind")) for constraint in definition.constraints]
            primary_key = exp.PrimaryKeyColumnConstraint in kinds
            unique = exp.UniqueColumnConstraint in kinds
            not_null = primary_key or exp.NotNullColumnConstraint in kinds
            if not_null:
                non_null.add(definition.name.lower())
            if primary_key or unique:
                unique_sets.append((definition.name,))
        for constraint in _table_constraints(symbol):
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
        ir.OutputColumn(origin=origin, name=column.name, scalar_expr=ir.ColumnRef(origin=origin, column=column))
        for column in inputs
    )


def _source_line(node: exp.Expression) -> int | None:
    """The first source line of `node`, from the positions SQLGlot keeps on its tokens."""
    lines = [descendant.meta["line"] for descendant in node.walk() if "line" in descendant.meta]
    return min(lines, default=None)


def _subquery_conjunct(conjunct: exp.Expression) -> tuple[ir.JoinKind, exp.Select, exp.Expression | None] | None:
    """A WHERE conjunct lowered as a filtering join: its kind, the subquery,
    and the IN operand (None for EXISTS)."""
    match conjunct:
        case exp.Exists(this=exp.Select() as select):
            return "semi", select, None
        case exp.Not(this=exp.Exists(this=exp.Select() as select)):
            return "anti", select, None
        case exp.In(this=operand, args={"query": exp.Subquery() as subquery}) if not isinstance(operand, exp.Tuple):
            body = _unwrap_query(subquery)
            return ("semi", body, operand) if isinstance(body, exp.Select) else None
        case _:
            return None


@cache
def _builtins(dialect: str) -> dict[type[exp.Expression], tuple[ir.Builtin, tuple[str, ...]]]:
    """Each modelled Builtin, by the SQLGlot node it lowers from (exactly that
    type: TRY_CAST, a subclass of CAST, may be NULL for a value), with the
    argument keys its parameters come from, a list key in place of a variadic
    Builtin's repeated parameter. Not `||`: Oracle reads NULL as '' there, and
    MySQL reads it as OR.

    Comparisons, LIKE, NOT, BETWEEN and the bitwise operators are never NULL
    for non-null arguments. Arithmetic and CAST only where they raise an error
    rather than return NULL (`_CHECKED_ARITHMETIC_DIALECTS`: SQLite turns a
    NaN into NULL, MySQL a bad date CAST), and division and remainder only
    where division by zero raises too (`_CHECKED_DIVISION_DIALECTS`). `+`, `-`
    and negation are injective where exact arithmetic overflows into an error."""
    checked = dialect in _CHECKED_ARITHMETIC_DIALECTS

    def builtin(name: str, arity: int, *, total: bool = True, injective: bool = False) -> ir.Builtin:
        return ir.Builtin(name=name, strict=(True,) * arity, total=(True,) * arity if total else None, injective=injective)

    binary = {
        exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=", exp.Like: "LIKE", exp.ILike: "ILIKE",
        exp.BitwiseAnd: "&", exp.BitwiseOr: "|", exp.BitwiseXor: "^", exp.BitwiseLeftShift: "<<", exp.BitwiseRightShift: ">>",
    }
    division = {exp.Div: "/", exp.IntDiv: "//", exp.Mod: "%"}
    return {
        **{node: (builtin(name, 2), ("this", "expression")) for node, name in binary.items()},
        exp.Mul: (builtin("*", 2, total=checked), ("this", "expression")),
        **{node: (builtin(name, 2, total=dialect in _CHECKED_DIVISION_DIALECTS), ("this", "expression")) for node, name in division.items()},
        **{node: (builtin(name, 2, total=checked, injective=checked), ("this", "expression")) for node, name in {exp.Add: "+", exp.Sub: "-"}.items()},
        exp.Neg: (builtin("unary -", 1, total=checked, injective=checked), ("this",)),
        exp.Not: (builtin("NOT", 1), ("this",)),
        exp.BitwiseNot: (builtin("~", 1), ("this",)),
        exp.Cast: (builtin("CAST", 1, total=checked), ("this",)),
        # `1 BETWEEN NULL AND 0` is FALSE.
        exp.Between: (ir.Builtin(name="BETWEEN", strict=(True, False, False), total=(True, True, True), injective=False), ("this", "low", "high")),
        # NULL values are skipped: `concat_ws(',', NULL)` is ''.
        exp.ConcatWs: (
            ir.BuiltinVariadic(name="concat_ws", strict=(True,), total=(True,), injective=False, repeated_strict=False, repeated_total=False),
            ("expressions",),
        ),
    }


# SQLite turns an overflowing integer into a REAL and a NaN into NULL, and
# MySQL outside strict mode saturates an integer: there `a + 1` may be equal
# for two `a`s, or NULL.
_CHECKED_ARITHMETIC_DIALECTS = {"duckdb", "postgres"}
# DuckDB's `n % 0` and `n // 0` are NULL.
_CHECKED_DIVISION_DIALECTS = {"postgres"}


def _as_list(value: exp.Expression | list[exp.Expression]) -> list[exp.Expression]:
    return value if isinstance(value, list) else [value]


def _disjuncts(predicate: exp.Expression) -> list[exp.Expression]:
    """The operands of a predicate's top-level ORs, parentheses removed."""
    while isinstance(predicate, exp.Paren):
        predicate = predicate.this
    if isinstance(predicate, exp.Or):
        return _disjuncts(predicate.this) + _disjuncts(predicate.expression)
    return [predicate]


def _and(parts: tuple[ir.ScalarExpr, ...], origin: Origin) -> ir.ScalarExpr | None:
    """The AND of `parts`: None for none, the part itself for one."""
    match parts:
        case ():
            return None
        case (part,):
            return part
        case _:
            return ir.And(origin=origin, parts=parts)


def _conjuncts(predicate: exp.Expression) -> list[exp.Expression]:
    """The operands of a predicate's top-level ANDs, parentheses removed."""
    while isinstance(predicate, exp.Paren):
        predicate = predicate.this
    if isinstance(predicate, exp.And):
        return _conjuncts(predicate.this) + _conjuncts(predicate.expression)
    return [predicate]


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
        for select in statement.find_all(exp.Select) if column_markers(select)
    ]



def _asserted_cases(ast: ParsedProgram) -> list[exp.Case]:
    statements = (*ast.create_statements, ast.root_select)
    return [
        case for statement in statements if statement is not None
        for case in statement.find_all(exp.Case) if exhaustive_line(case) is not None
    ]


def _table_level_unique_columns(constraint: exp.Expression) -> tuple[str, ...] | None:
    if isinstance(constraint, exp.PrimaryKey):
        return tuple(identifier.name for identifier in constraint.expressions)
    if isinstance(constraint, exp.UniqueColumnConstraint) and isinstance(constraint.this, exp.Schema):
        return tuple(identifier.name for identifier in constraint.this.expressions)
    return None


def _table_constraints(symbol: _Symbol) -> list[exp.Expression]:
    """A declared table's table-level constraints, named or not: its CREATE
    TABLE's, then those added later."""
    assert symbol.statement is not None
    schema = symbol.statement.this
    own = _unwrap_constraints(schema.expressions) if isinstance(schema, exp.Schema) else []
    return own + symbol.constraints


def _unwrap_constraints(constraints: Iterable[exp.Expression]) -> list[exp.Expression]:
    """`CONSTRAINT name ...` is the constraints it names."""
    return [
        inner for constraint in constraints
        for inner in (constraint.expressions if isinstance(constraint, exp.Constraint) else (constraint,))
    ]


def _is_added_constraint(constraint: exp.Expression) -> bool:
    return isinstance(constraint, exp.PrimaryKey | exp.ForeignKey) or _table_level_unique_columns(constraint) is not None


def _references(symbol: _Symbol) -> list[tuple[tuple[str, ...], exp.Table, tuple[str, ...] | None, exp.Expression]]:
    """Every `REFERENCES` constraint of a table, column-level and table-level,
    as `(columns, referenced table, referenced columns, node)`. Referenced
    columns are None when omitted, meaning the referenced primary key."""
    found = []
    assert symbol.statement is not None
    for definition in symbol.statement.this.find_all(exp.ColumnDef):
        for constraint in definition.constraints:
            reference = constraint.args.get("kind")
            if isinstance(reference, exp.Reference):
                found.append(((definition.name,), *_reference_target(reference), definition))
    for foreign_key in _table_constraints(symbol):
        if not isinstance(foreign_key, exp.ForeignKey):
            continue
        reference = foreign_key.args.get("reference")
        if isinstance(reference, exp.Reference):
            columns = tuple(identifier.name for identifier in foreign_key.expressions)
            found.append((columns, *_reference_target(reference), foreign_key))
    return found


def _reference_target(reference: exp.Reference) -> tuple[exp.Table, tuple[str, ...] | None]:
    target = reference.this
    if isinstance(target, exp.Schema):
        return target.this, tuple(identifier.name for identifier in target.expressions)
    return target, None


def _primary_key(symbol: _Symbol) -> tuple[str, ...] | None:
    assert symbol.statement is not None
    for definition in symbol.statement.this.find_all(exp.ColumnDef):
        if any(isinstance(constraint.args.get("kind"), exp.PrimaryKeyColumnConstraint) for constraint in definition.constraints):
            return (definition.name,)
    for constraint in _table_constraints(symbol):
        if isinstance(constraint, exp.PrimaryKey):
            return tuple(identifier.name for identifier in constraint.expressions)
    return None


def _missing(relation: ir.RelationExpr, names: Iterable[str]) -> list[str]:
    known = {column.name.lower() for column in relation.output_columns}
    return [name for name in names if name.lower() not in known]


_SUPPORTED_CREATE_KINDS = {"TABLE", "VIEW"}

# Dialects whose default collation compares text by identity; MySQL's and
# SQL Server's ignore case, so distinct strings may compare equal there.
_BINARY_COLLATION_DIALECTS = {"duckdb", "postgres"}

_TEXT_TYPES = {exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT}


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


_SET_OPERATORS: dict[type[exp.Expression], ir.SetOperator] = {exp.Union: "union", exp.Intersect: "intersect", exp.Except: "except"}


# Types whose equal values are identical: no collation (text has a comparison
# type only when compared by identity), no signed zero or NaN, no two
# spellings of one interval.
_IDENTITY_TYPES = {
    *exp.DataType.INTEGER_TYPES, *exp.DataType.TEXT_TYPES,
    exp.DataType.Type.DECIMAL, exp.DataType.Type.BOOLEAN, exp.DataType.Type.DATE, exp.DataType.Type.TIME,
    exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.UUID,
}

_EXACT_NUMERIC_TYPES = {*exp.DataType.INTEGER_TYPES, exp.DataType.Type.DECIMAL}

# Nodes whose value is not a function of the columns an expression reads.
_NOT_A_FUNCTION_OF_ITS_COLUMNS = (
    exp.Anonymous, exp.Rand, exp.Randn, exp.Uuid, exp.AggFunc, exp.Window, exp.Query, exp.Subquery,
    exp.CurrentDate, exp.CurrentDatetime, exp.CurrentTime, exp.CurrentTimestamp, exp.Placeholder, exp.Parameter, exp.Star,
)


def _enum_type_values(definition: exp.ColumnDef) -> frozenset[ir.LiteralValue] | None:
    """The members of a column declared `ENUM('a', ...)`: its only non-null values."""
    kind = definition.args.get("kind")
    if not (isinstance(kind, exp.DataType) and kind.this == exp.DataType.Type.ENUM and kind.expressions):
        return None
    values = [literal_value(member) for member in kind.expressions]
    if any(value is None or value.type != "string" for value in values):
        return None
    return frozenset(value for value in values if value is not None)


def _is_scalar_aggregate(select: exp.Select) -> bool:
    """An aggregate with no GROUP BY: an aggregate function outside any window."""
    return select.args.get("qualify") is None and any(
        isinstance(node, exp.AggFunc) and node.find_ancestor(exp.Window, exp.Select) is select
        for item in select.expressions for node in item.find_all(exp.AggFunc)
    )


def _is_positional(join: exp.Join) -> bool:
    return (join.args.get("method") or "").lower() == "positional"


def _join_kind(join: exp.Join) -> ir.JoinKind:
    """SEMI, ANTI and CROSS whatever the side; otherwise the side, else INNER
    (`STRAIGHT_JOIN`, and ASOF, which keeps a subset of the inner join's rows).
    Any other kind (ClickHouse `ARRAY JOIN`) is read as CROSS, which pairs rows
    and proves nothing from the condition beyond an inner join's."""
    if _is_positional(join):
        return "full"
    match (join.args.get("kind") or "").lower(), (join.args.get("side") or "").lower():
        case ("semi" | "anti" | "cross") as kind, _:
            return kind
        case _, ("left" | "right" | "full") as side:
            return side
        case "outer", _:
            return "full"
        case "" | "inner" | "straight_join", _:
            return "inner"
        case _:
            return "cross"


def _join_label(join: exp.Join, dialect: str) -> str:
    keywords = " ".join(part for part in (join.side, join.kind, "JOIN") if part)
    source = join.this
    if isinstance(source, exp.Subquery):
        return f"{keywords} subquery {source.alias}".rstrip()
    return f"{keywords} {source.sql(dialect=dialect)}"


def _select_label(select: exp.Select) -> str:
    node = select.parent
    while node is not None:
        match node:
            case exp.CTE():
                return f"CTE {node.alias}"
            case exp.Create():
                table = node.this.this if isinstance(node.this, exp.Schema) else node.this
                return f"view {table.name}"
            case exp.Subquery() if node.alias:
                return f"subquery {node.alias}"
            case exp.Subquery():
                return "subquery"
            case _:
                node = node.parent
    return "Root Select"
