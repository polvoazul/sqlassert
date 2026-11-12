"""Link a parsed SQL program into the framework-independent relational IR.

Declaration and scope discovery happens before lowering. This gives table,
view, and CTE references compiler-like symbol resolution: every occurrence is
a fresh Alias, while all occurrences share one memoized NamedRelation.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence, Set
from dataclasses import dataclass, field, replace
from functools import cache
from typing import Literal, TypeGuard, assert_never

from sqlglot import Expr, exp

from sqlassert import diagnostics as diag
from sqlassert import ir
from sqlassert.dialects import DialectSemantics, semantics
from sqlassert.diagnostics import Diagnostic, quoted, quoted_all
from sqlassert.properties import (
    EnumeratedDomain, ExactlyOneJoin, ExhaustiveCase, ForeignKey, FunctionalDependency, ManyToOneJoin, NonNullColumn, NonNullColumns, NonNullElements, NonNullUniqueSet,
    OneToManyJoin, OneToOneJoin, Property, ReferentialCoverage, UniqueJoin,
    UniqueSet,
)
from sqlassert.provenance import AttachmentSite, Origin, SQL
from sqlassert.sql_parse import (
    JOIN_MARKER_NOUNS, MARKER_NOUNS, ColumnMarker, ParsedProgram, assertion_span, column_markers,
    exhaustive_span, grouping, join_marker_kind, join_origin, literal_value, marker_position,
    written_sql,
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
    column_aliases: tuple[str, ...] = ()
    required_columns: set[str] = field(default_factory=set)
    # Table-level constraints added after CREATE TABLE (ALTER TABLE, CREATE
    # UNIQUE INDEX), shaped like the CREATE TABLE's own, and columns set NOT NULL.
    constraints: list[Expr] = field(default_factory=list)
    not_null: set[str] = field(default_factory=set)
    # Where the SQL first reads it: an undeclared table's warning points there.
    first_line: int | None = None
    # Each unique index with an expression key part: its parts and statement.
    expression_keys: list[tuple[list[Expr], exp.Create]] = field(default_factory=list)


@dataclass(frozen=True)
class _Binding:
    qualifier: str
    name: str
    column: ir.OutputColumn
    # The schema (and catalog) of an unaliased table, as `s1` in `s1.t.k`.
    schema: str = ""
    # Hidden from unqualified names: a USING column's input sides, which an
    # unqualified name reads merged (`_merge_using`).
    shadowed: bool = False


@dataclass(frozen=True)
class _MarkerReference:
    """A Foreign Key marker whose referencing columns are lowered, awaiting its referenced relation."""

    marker: ColumnMarker
    kind: Literal["foreign-key", "declared-foreign-key"]
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
    # The `REFERENCES` or `FOREIGN KEY` constraint itself.
    node: Expr


@dataclass(frozen=True)
class _Lowered:
    relation: ir.RelationExpr
    bindings: tuple[_Binding, ...]


@dataclass
class IrParser:
    dialect: str
    # Unenforced keys and foreign keys become Property Declarations, as `RELY` makes them.
    trust_unenforced_keys: bool = True
    _global_symbols: dict[str, _Symbol] = field(default_factory=dict)
    _symbols: list[_Symbol] = field(default_factory=list)
    _query_scopes: dict[int, dict[str, _Symbol]] = field(default_factory=dict)
    _table_symbols: dict[int, _Symbol] = field(default_factory=dict)
    _source_required: dict[int, set[str]] = field(default_factory=dict)
    _source_expressions: dict[int, Expr] = field(default_factory=dict)
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
    # Each scalar subquery lowered as a join, by id, and the qualifier of the
    # binding that holds its value.
    _scalar_values: dict[int, str] = field(default_factory=dict)
    # Each aggregate call and grouping-determined column of the grouped
    # select item being lowered, by id, and its value per group (`_lower_grouped`).
    _grouped_values: dict[int, ir.ScalarExpr] = field(default_factory=dict)
    # Each set-returning call of a select list, by id, and the qualifier of the
    # binding that holds the values it returns.
    _set_returning_values: dict[int, str] = field(default_factory=dict)
    _scalar_subqueries: list[tuple[AttachmentSite, ir.Join]] = field(default_factory=list)
    _sort_keys: dict[int, tuple[ir.OutputColumn, ...] | None] = field(default_factory=dict)
    _site_sort_keys: list[tuple[AttachmentSite, tuple[ir.OutputColumn, ...] | None]] = field(default_factory=list)
    _set_operations: list[tuple[AttachmentSite, ir.SetOperation]] = field(default_factory=list)
    _unsupported: list[tuple[ir.Node, str]] = field(default_factory=list)
    # Columns made up for names a query uses: of a table with no CREATE, or an
    # opaque relation (`_guessed`), and those a declared table does not have (`_absent`).
    _guessed: set[ir.OutputColumn] = field(default_factory=set)
    # A scalar subquery's columns, each bound to the scope that owns it.
    _scoped_columns: dict[int, ir.OutputColumn] = field(default_factory=dict)
    _absent: set[ir.OutputColumn] = field(default_factory=set)
    # Each declared table column's type, as its CREATE TABLE writes it.
    _declared_types: dict[ir.OutputColumn, exp.DataType] = field(default_factory=dict)
    # The comparisons already checked for a zoned timestamp's text, by id.
    _zone_checked: set[int] = field(default_factory=set)
    # Each composite column or CAST a comparison reads, by id, with the
    # comparison: a warning unless its elements are declared non-null.
    _composite_comparisons: dict[int, tuple[Origin, str, str, ir.ScalarExpr]] = field(default_factory=dict)
    _marker_targets: dict[int, _Symbol] = field(default_factory=dict)
    _marker_references: list[_MarkerReference] = field(default_factory=list)
    _constraint_references: list[_ConstraintReference] = field(default_factory=list)
    # Constraints an ALTER TABLE ... NOT VALID added: rows already there were never checked.
    _unvalidated: set[int] = field(default_factory=set)

    @property
    def semantics(self) -> DialectSemantics:
        return semantics(self.dialect)

    def parse(self, ast: ParsedProgram) -> IrConversionResult:
        for statement in (*ast.create_statements, ast.root_select):
            if statement is None:
                continue
            for node in statement.find_all(exp.Identifier):
                if self.dialect == "postgres" and node.quoted and node.name != node.name.lower() and node.find_ancestor(exp.Collate, exp.CollateColumnConstraint) is None:
                    diagnostic = Diagnostic(diag.UNSUPPORTED_CONSTRUCT, "case-sensitive quoted PostgreSQL identifiers are not supported", self._origin(node))
                    return IrConversionResult(ir.Program(), (), (diagnostic,))
            for table in statement.find_all(exp.Table):
                if any("." in part.name for part in table.parts if isinstance(part, exp.Identifier)):
                    diagnostic = Diagnostic(diag.UNSUPPORTED_CONSTRUCT, "dots inside quoted relation identifiers are not supported", self._origin(table))
                    return IrConversionResult(ir.Program(), (), (diagnostic,))
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
        # Every view is a query of its own: its markers are checked whether or
        # not the root reads it, so a file of views (a project's models) needs
        # no root SELECT.
        for symbol in tuple(self._symbols):
            if symbol.role is ir.RelationRole.VIEW and symbol not in self._named_relations:
                self._resolve_symbol(symbol)
        # Referenced relations are linked last: a marker on a view may name
        # the view itself, or one that reads it.
        self._link_constraint_references()
        self._link_marker_references()
        self._report_unanalyzed(ast)

        named_relations = tuple(self._named_relations.get(symbol) or self._unresolved_named_relation(symbol) for symbol in self._symbols)
        warnings = [*self._undeclared_table_warnings(), *self._warnings, *self._composite_warnings()]
        return IrConversionResult(
            ir.Program(named_relations=named_relations, root=root, assertions=tuple(self._assertions), declarations=tuple(self._declarations), attachment_sites=tuple(self._attachment_sites),
                scalar_subqueries=tuple(self._scalar_subqueries), sort_keys=tuple(self._site_sort_keys),
                set_operations=tuple(self._set_operations), unsupported=tuple(self._unsupported)),
            tuple(self._knowledge), tuple(self._diagnostics), tuple(dict.fromkeys(warnings)),
        )

    def _undeclared_table_warnings(self) -> list[Diagnostic]:
        return [
            Diagnostic(
                diag.UNDECLARED_TABLE,
                f"table {symbol.name} has no CREATE TABLE in the program, so its keys, nullability and column types are unknown: "
                "declare it with CREATE TABLE, or analyze with a database connection (analyze_database)",
                replace(symbol.origin, line=symbol.first_line or symbol.origin.line),
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
        aliases = tuple(column.name for column in statement.this.expressions) if role is ir.RelationRole.VIEW and isinstance(statement.this, exp.Schema) else ()
        symbol = _Symbol(name=name, role=role, origin=origin, body=query, statement=statement, column_aliases=aliases)
        self._global_symbols[key] = symbol
        self._symbols.append(symbol)

    def _declare_index(self, statement: exp.Create) -> None:
        """A unique index over plain columns is a Unique Set, like UNIQUE; one
        with an expression key part may be (`_expression_key`); a partial one,
        or any other index, states nothing about the data."""
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
        if not columns or params.args.get("where") is not None:
            # Uniqueness of only some rows is not a Unique Set.
            self._unsupported_create(statement, "a partial unique index")
            return
        if len(plain) != len(columns):
            parts = [column.this if isinstance(column, exp.Ordered) else column for column in columns]
            symbol.expression_keys.append(([part.unnest() for part in parts], statement))
            return
        schema = exp.Schema(expressions=[exp.to_identifier(column.name) for column in plain])
        symbol.constraints.append(exp.UniqueColumnConstraint(this=schema))

    def _alter(self, statement: exp.Alter) -> None:
        """ALTER TABLE adding PRIMARY KEY, UNIQUE, FOREIGN KEY or CHECK
        constraints, or setting columns NOT NULL. Any other action could invalidate what is
        known of the table, so a statement with one states nothing."""
        actions = statement.args.get("actions") or ()
        added: list[Expr] = []
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
                f"only ALTER TABLE adding PRIMARY KEY, UNIQUE, FOREIGN KEY or CHECK constraints, or setting NOT NULL, "
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
        if statement.args.get("not_valid"):
            self._unvalidated.update(id(constraint) for constraint in added)

    def _created_before(self, table: exp.Table, statement: Expr) -> _Symbol | None:
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

    def _discover_marker_targets(self, query: exp.Query, scope: dict[str, _Symbol]) -> None:
        """The relation each Foreign Key marker on `query` references."""
        for marker in column_markers(query):
            if marker.referenced_relation is not None:
                origin = Origin.marked(marker.referenced_relation, marker.span)
                target = self._symbol_for_name(marker.referenced_relation, scope, origin)
                self._marker_targets[id(marker)] = target
                self._require_undeclared(target, marker.referenced_columns)

    def _discover_query(self, query: exp.Query, inherited_scope: dict[str, _Symbol]) -> None:
        if id(query) in self._query_scopes:
            return

        scope = dict(inherited_scope)
        with_clause = _arg(query, "with")
        if with_clause is not None:
            bodies = []
            recursive = bool(with_clause.args.get("recursive"))
            forward = recursive and self.dialect != "duckdb"
            for cte in with_clause.expressions:
                body = _unwrap_query(cte.this)
                duckdb_recursive = recursive and not forward and isinstance(body, exp.Union)
                if not forward:
                    if duckdb_recursive and isinstance(body, exp.Union):
                        # The seed is outside the recursive name's scope.
                        seed = _unwrap_query(body.this)
                        if seed is not None and _arg(body, "with") is None:
                            self._discover_query(seed, scope)
                    elif body is not None:
                        self._discover_query(body, scope)
                symbol = _Symbol(
                    name=cte.alias,
                    role=ir.RelationRole.CTE,
                    origin=Origin(SQL, cte.sql(dialect=self.dialect)),
                    body=body,
                    column_aliases=_column_aliases(cte),
                )
                scope[cte.alias.lower()] = symbol
                self._symbols.append(symbol)
                if forward and body is not None:
                    bodies.append(body)
                elif duckdb_recursive and body is not None:
                    self._discover_query(body, scope)
            for body in bodies:
                self._discover_query(body, scope)

        self._query_scopes[id(query)] = scope
        if isinstance(query, exp.SetOperation):
            # SQLGlot 30 wraps a nested set operation in a Subquery.
            for side in (query.this, query.expression):
                inner = _unwrap_query(side)
                if inner is not None:
                    self._discover_query(inner, scope)
            self._discover_marker_targets(query, scope)
            return
        if not isinstance(query, exp.Select):
            return

        sources = _direct_sources(query)
        by_qualifier: dict[str, list[Expr]] = {}
        for source in [*sources, *(hidden for source in sources for hidden in _hidden_sources(source))]:
            self._source_required.setdefault(id(source), set())
            self._source_expressions[id(source)] = source
            qualifier = source.alias_or_name.lower()
            if qualifier:
                by_qualifier.setdefault(qualifier, []).append(source)
            if _is_named_table(source):
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
            elif len(sources) == 1 and column.find_ancestor(exp.Select) is query:
                # An unqualified column in a nested query belongs to that query's scope.
                self._require(sources[0], column.name)

        for joins, level in _join_levels(query):
            for join in joins:
                for identifier in join.args.get("using") or ():
                    for source in level:
                        self._require(source, identifier.name)

        if len(sources) == 1 and _star_only(query):
            for marker in column_markers(query):
                for column in (*marker.columns, *marker.dependent):
                    self._require(sources[0], column)

        self._discover_marker_targets(query, scope)

        where = query.args.get("where")
        for conjunct in _conjuncts(where.this) if where is not None else ():
            matched = _subquery_conjunct(conjunct)
            if matched is not None:
                self._discover_query(matched[1], scope)
        for expression in (*(_conjuncts(where.this) if where is not None else ()), *query.expressions):
            for subquery in _scalar_subqueries(expression):
                if isinstance(subquery.this, exp.Select):
                    self._discover_query(subquery.this, scope)

    def _symbol_for_table(self, table: exp.Table, scope: dict[str, _Symbol]) -> _Symbol:
        symbol = self._symbol_for_name(_qualified_name(table), scope, Origin(SQL, table.sql(dialect=self.dialect)))
        if symbol.first_line is None:
            symbol.first_line = _source_line(table)
        return symbol

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
                    _ConstraintReference(symbol, columns, target, target_columns, self._origin(node.find_ancestor(exp.ColumnDef) or node), node)
                )

    def _require(self, source: Expr, column: str) -> None:
        self._source_required.setdefault(id(source), set()).add(column)
        if _is_named_table(source) and column.lower() not in {name.lower() for name in _column_aliases(source)}:
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
                        aliases = {name.lower() for name in symbol.column_aliases}
                        for column in symbol.required_columns:
                            if column.lower() not in aliases:
                                self._require(sources[0], column)
            for source_id, columns in tuple(self._source_required.items()):
                source = self._source_expressions[source_id]
                body = _unwrap_query(source) if isinstance(source, exp.Subquery) else None
                if isinstance(body, exp.Select) and _star_only(body):
                    nested_sources = _direct_sources(body)
                    if len(nested_sources) == 1:
                        aliases = {name.lower() for name in _column_aliases(source)}
                        for column in columns:
                            if column.lower() not in aliases:
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
            origin = self._origin(query)
            by_name = bool(query.args.get("by_name"))
            if by_name:
                names: dict[str, str] = {}
                for column in (*left.output_columns, *right.output_columns):
                    names.setdefault(column.name.lower(), column.name)
                output_columns = self._opaque_output_columns(names.values(), origin)
            else:
                output_columns = _pass_output_columns(left.output_columns, origin)
            relation = ir.SetOperation(
                origin=self._origin(query), output_columns=output_columns, is_schema_complete=left.is_schema_complete and right.is_schema_complete,
                operator=_SET_OPERATORS[type(query)], left=left, right=right, distinct=bool(query.args.get("distinct")),
                by_name=by_name,
            )
            last = _last_select(query.this)
            position = marker_position(last) if last is not None else None
            if relation.operator != "union" and position is not None:
                self._set_operations.append((AttachmentSite(relation.operator.upper(), position[1], position[0], True), relation))
            limited = self._lower_limit(relation, query)
            # A marker after the last token of the query marks the whole of it,
            # so the last operand, which ends there too, has no site of its own.
            self._register_column_markers(limited, query)
            end = marker_position(query)
            if end is not None:
                self._attachment_sites = [
                    (site, other) for site, other in self._attachment_sites if not (site.trailing and site.offset == end[0])
                ]
            self._add_attachment_site(query, _select_label(query), limited, trailing=True)
            return limited
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
        elif any(join.args.get("method") == "NATURAL" for join in _all_joins(select)):
            relation = self._opaque_relation(select, _selected_output_names(select))
        else:
            lowered = self._lower_source(source)
            for join in select.args.get("joins") or ():
                lowered = self._lower_join(lowered, join)
            relation = self._lower_select_tail(lowered, select)
        return self._finish_select(relation, select)

    def _finish_select(self, relation: ir.RelationExpr, select: exp.Select) -> ir.RelationExpr:
        relation = self._lower_limit(relation, select)
        self._register_column_markers(relation, select)
        self._add_attachment_site(select, _select_label(select), relation, trailing=True)
        position = marker_position(select)
        if position is not None:
            offset, line = position
            self._site_sort_keys.append((AttachmentSite(_select_label(select), line, offset, True), self._sort_keys.get(id(select))))
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

    def _lower_source(self, source: Expr) -> _Lowered:
        if any(source.args.get(key) for key in ("pivots", "sample", "version", "changes")):
            relation = self._opaque_relation(source, self._source_required.get(id(source), set()))
            self._unsupported.append((relation, "a FROM modifier that changes the input rows"))
            return self._alias(relation, source.alias_or_name, self._origin(source))
        if _is_named_table(source):
            symbol = self._table_symbols[id(source)]
            alias_name = source.alias or source.name
            # An unaliased table may be named with its schema: `s1.t.k`.
            schema = "" if source.alias else _schema_path(source)
            if symbol in self._resolving:
                if symbol.role is ir.RelationRole.VIEW:
                    self._report_cycle(symbol, source)
                recursive = self._recursive_relation(symbol, source)
                return self._alias(recursive, alias_name, self._origin(source), schema)
            return self._alias(self._resolve_symbol(symbol), alias_name, self._origin(source), schema, _column_aliases(source))

        nested = _nested_join(source) or _aliased_join(source)
        if nested is not None:
            first, joins = nested
            lowered = self._lower_source(first)
            for join in joins:
                lowered = self._lower_join(lowered, join)
            return lowered if source.alias == "" else self._alias_join(lowered.relation, source)

        if isinstance(source, exp.Subquery):
            relation = self._lower_query(source)
            if relation is not None:
                # DuckDB allows an unaliased FROM subquery; nothing can qualify
                # a column by its name, so a placeholder serves.
                return self._alias(relation, source.alias or "subquery", self._origin(source), column_aliases=_column_aliases(source))

        if isinstance(source, exp.Values):
            values = self._lower_values(source)
            if values is not None:
                return self._alias(values, source.alias_or_name, self._origin(source))

        opaque = self._opaque_relation(source, self._source_required.get(id(source), set()))
        construct = _unsupported_source(source)
        if construct is not None:
            self._unsupported.append((opaque, construct))
        return self._alias(opaque, source.alias_or_name or "opaque", self._origin(source))

    def _alias_join(self, join: ir.RelationExpr, source: Expr) -> _Lowered:
        """`(a JOIN b ...) AS x`: a relation of its own, which hides the names
        of a and b and has each of their columns under its own name. A name
        two columns share each dialect resolves differently (DuckDB renames
        the second `x_1`, others reject reading it), so its columns are
        opaque, the first one readable by that name; every name is, when a
        source's columns are not all known."""
        origin = self._origin(source)
        counts = Counter(column.name.lower() for column in join.output_columns)
        relation = join
        if not join.is_schema_complete or any(count > 1 for count in counts.values()):
            outputs = tuple(
                ir.OutputColumn(
                    origin=origin, name=column.name,
                    scalar_expr=ir.ColumnRef(origin=origin, column=column) if join.is_schema_complete and counts[column.name.lower()] == 1
                    else ir.OpaqueExpression(origin=origin, description=f"{source.alias}.{column.name}"),
                )
                for column in join.output_columns
            )
            relation = ir.Project(origin=origin, output_columns=outputs, is_schema_complete=join.is_schema_complete, input=join)
        lowered = self._alias(relation, source.alias, origin, column_aliases=_column_aliases(source))
        bindings: dict[str, _Binding] = {}
        for binding in lowered.bindings:
            bindings.setdefault(binding.name, binding)
        return _Lowered(lowered.relation, tuple(bindings.values()))

    def _lower_values(self, values: exp.Values) -> ir.OpaqueRelation | None:
        """`(VALUES (...), ...) AS t (a, ...)`: rows of literals, stated as
        Knowledge of its columns. A column is non-null when no row holds NULL
        there; its Enumerated Domain is its values, up to `_VALUES_DOMAIN_ROWS`
        rows (past that, a closed set is more noise than fact); it is a Unique
        Set when no two of its values may compare equal (`1` and `1.0` may,
        and `'a'` and `'A'` unless text compares by identity), and so are all
        columns together when no two rows may. A column with any other
        expression states nothing. Without column names it stays opaque."""
        from sqlassert.rules.domain import _may_coincide

        alias = values.args.get("alias")
        names = [column.name for column in alias.columns] if isinstance(alias, exp.TableAlias) else []
        rows = [row.expressions if isinstance(row, exp.Tuple) else [row] for row in values.expressions]
        if not names or not rows or any(len(row) != len(names) for row in rows):
            return None
        origin = self._origin(values)
        columns = tuple(
            ir.OutputColumn(origin=origin, name=name, scalar_expr=ir.OpaqueExpression(origin=origin, description=f"VALUES column {name}"))
            for name in names
        )
        binary = self.semantics.text_is_binary

        def distinct(items: list[ir.LiteralValue | None]) -> bool:
            # NULL equals nothing, so it never repeats (a lax Unique Set).
            known = [item for item in items if item is not None]
            return all(not _may_coincide(first, second, binary) for index, first in enumerate(known) for second in known[index + 1:])

        literal_rows: list[list[ir.LiteralValue | None]] = []
        for row in rows:
            literal_rows.append([None if isinstance(item, exp.Null) else literal_value(item) for item in row])
        for index, column in enumerate(columns):
            written = [row[index] for row in rows]
            if not all(isinstance(item, exp.Null) or literal_value(item) is not None for item in written):
                continue  # Some value is an expression: nothing is stated.
            items = [row[index] for row in literal_rows]
            if not any(isinstance(item, exp.Null) for item in written):
                self._knowledge.append(NonNullColumn(column=column))
            present = frozenset(item for item in items if item is not None)
            if present and len(rows) <= _VALUES_DOMAIN_ROWS:
                self._knowledge.append(EnumeratedDomain(column=column, values=present))
            if distinct(items):
                self._knowledge.append(UniqueSet(columns=frozenset({column})))
        literal = all(all(isinstance(item, exp.Null) or literal_value(item) is not None for item in row) for row in rows)
        if literal and len(columns) > 1 and all(
            any(one is None or other is None or not _may_coincide(one, other, binary) for one, other in zip(first, second, strict=True))
            for position, first in enumerate(literal_rows) for second in literal_rows[position + 1:]
        ):
            self._knowledge.append(UniqueSet(columns=frozenset(columns)))
        return ir.OpaqueRelation(origin=origin, output_columns=columns, is_schema_complete=True, description=values.sql(dialect=self.dialect))

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
            self._table_knowledge(named, symbol)
            return named

        self._resolving.add(symbol)
        try:
            body = self._lower_query(symbol.body) if symbol.body is not None else None
        finally:
            self._resolving.discard(symbol)
        if body is None:
            body = self._opaque_relation(symbol.body or exp.Table(this=symbol.name), symbol.required_columns)
        output_columns = self._aliased_outputs(body, symbol.origin, symbol.column_aliases)
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
            self._table_knowledge(named, symbol)
        return named

    def _table_output_columns(self, symbol: _Symbol) -> tuple[tuple[ir.OutputColumn, ...], bool]:
        definitions = list(symbol.statement.this.find_all(exp.ColumnDef)) if symbol.statement is not None else []
        names = [definition.name for definition in definitions]
        seen = {name.lower() for name in names}
        names.extend(column for column in sorted(symbol.required_columns, key=str.lower) if column.lower() not in seen)
        complete = symbol.statement is not None and {column.lower() for column in symbol.required_columns} <= seen
        columns = self._opaque_output_columns(names, symbol.origin)
        by_name = {definition.name: definition for definition in definitions}
        if symbol.statement is not None:
            self._absent.update(column for column in columns if column.name not in by_name)
        # A column declared with a COLLATE compares under a collation sqlassert
        # does not model: it is left untyped, so its values are unknown.
        collated = {name for name, definition in by_name.items() if _collated(definition)}
        for column in columns:
            if column.name in collated:
                self._unsupported.append((column, "COLLATE"))
        by_name = {name: definition for name, definition in by_name.items() if name not in collated}
        typed = tuple(
            replace(
                column, binary_text=self._is_binary_text(by_name[column.name]),
                comparison_type=self._comparison_type(by_name[column.name]),
                identity_comparison=self._identity_comparison(by_name[column.name]),
                exact_numeric=self._exact_numeric(by_name[column.name]),
            )
            if column.name in by_name else column for column in columns
        )
        for column in typed:
            kind = by_name[column.name].args.get("kind") if column.name in by_name else None
            if isinstance(kind, exp.DataType):
                self._declared_types[column] = kind
        return typed, complete

    def _comparison_type(self, definition: exp.ColumnDef) -> str | None:
        kind = definition.args.get("kind")
        if not isinstance(kind, exp.DataType):
            return None
        if kind.this in exp.DataType.TEXT_TYPES:
            return kind.sql(dialect=self.dialect) if self._is_binary_text(definition) else None
        if self._unscaled_decimal(kind):
            return None
        return kind.sql(dialect=self.dialect)

    def _identity_comparison(self, definition: exp.ColumnDef) -> bool:
        """Declared with a type whose equal values are identical (text only
        when compared by identity, which `_comparison_type` already checks)."""
        kind = definition.args.get("kind")
        if not (self._comparison_type(definition) is not None and isinstance(kind, exp.DataType)):
            return False
        if kind.this == exp.DataType.Type.TIMESTAMPTZ:
            return self.semantics.zoned_timestamp_is_instant
        return kind.this in _IDENTITY_TYPES

    def _exact_numeric(self, definition: exp.ColumnDef) -> bool:
        kind = definition.args.get("kind")
        return isinstance(kind, exp.DataType) and kind.this in _EXACT_NUMERIC_TYPES and not self._unscaled_decimal(kind)

    def _unscaled_decimal(self, kind: exp.DataType) -> bool:
        """A Postgres NUMERIC with no scale: 1.0 and 1.00 are equal values that differ."""
        return self.semantics.unscaled_decimal and kind.this == exp.DataType.Type.DECIMAL and not kind.expressions

    def _is_binary_text(self, definition: exp.ColumnDef) -> bool:
        """Declared VARCHAR or TEXT in a dialect whose default collation is
        binary: compared by identity. CHAR is not: it ignores trailing spaces
        when compared."""
        kind = definition.args.get("kind")
        return isinstance(kind, exp.DataType) and kind.this in _TEXT_TYPES and self.semantics.text_is_binary

    def _opaque_output_columns(self, names: Iterable[str], origin: Origin) -> tuple[ir.OutputColumn, ...]:
        if isinstance(names, Set):
            names = sorted(names, key=lambda name: (name.lower(), name))
        columns = tuple(
            ir.OutputColumn(
                origin=origin,
                name=name,
                scalar_expr=ir.OpaqueExpression(origin=origin, description=f"source column {name}"),
            )
            for name in names
        )
        self._guessed.update(columns)
        return columns

    def _recursive_relation(self, symbol: _Symbol, source: Expr) -> ir.RecursiveRelation:
        origin = self._origin(source)
        relation = ir.RecursiveRelation(
            origin=origin,
            output_columns=self._opaque_output_columns(symbol.required_columns, origin),
            is_schema_complete=False,
            description=source.sql(dialect=self.dialect),
            relation_name=symbol.name,
        )
        self._unsupported.append((relation, "a recursive CTE or view"))
        return relation

    def _aliased_outputs(self, source: ir.RelationExpr, origin: Origin, names: tuple[str, ...]) -> tuple[ir.OutputColumn, ...]:
        if names and not source.is_schema_complete:
            # Guessed source columns have no reliable positions.
            return self._opaque_output_columns(names, origin)
        return tuple(
            ir.OutputColumn(origin=origin, name=names[index] if index < len(names) else column.name, scalar_expr=ir.ColumnRef(origin=origin, column=column))
            for index, column in enumerate(source.output_columns)
        )

    def _alias(self, source: ir.RelationExpr, name: str, origin: Origin, schema: str = "", column_aliases: tuple[str, ...] = ()) -> _Lowered:
        output_columns = self._aliased_outputs(source, origin, column_aliases)
        alias = ir.Alias(origin=origin, output_columns=output_columns, is_schema_complete=source.is_schema_complete, source=source, name=name)
        return _Lowered(alias, tuple(_Binding(name.lower(), output.name.lower(), output, schema) for output in output_columns))

    def _lower_join(self, left: _Lowered, join: exp.Join) -> _Lowered:
        right = self._lower_source(join.this)
        marked = assertion_span(join)
        origin = self._origin(join, marked.line if marked is not None else None)
        kind = _join_kind(join, self.dialect)
        if kind is None:
            return self._opaque_join(left, right, join, origin)
        predicate = self._lower_join_predicate(join, left, right)
        if kind == "semi" or kind == "anti":
            # RIGHT SEMI and RIGHT ANTI keep right rows: the same join, inputs swapped.
            kept, other = (right, left) if (join.args.get("side") or "").lower() == "right" else (left, right)
            filtered = self._filtering_join(kept, other.relation, kind, predicate, origin)
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
            kind=kind,
            left=left.relation,
            right=right.relation,
            predicate=predicate,
        )
        bindings = tuple(
            replace(binding, column=remapped[binding.column])
            for binding in left.bindings + right.bindings
        )
        self._join_marker(join, relation)
        self._add_attachment_site(join, _join_label(join, self.dialect), relation, trailing=False)
        lowered = _Lowered(relation, bindings)
        return self._merge_using(lowered, join, kind, len(left.bindings), origin) if join.args.get("using") else lowered

    def _merge_using(self, lowered: _Lowered, join: exp.Join, kind: ir.JoinKind, left_count: int, origin: Origin) -> _Lowered:
        """`JOIN ... USING (k)` outputs one `k`: the left input's for an INNER
        or LEFT JOIN (equal to the right's wherever both exist), the right's
        for a RIGHT JOIN, and `coalesce(left.k, right.k)` for a FULL JOIN,
        where either side may be padded. An unqualified `k` reads it, so a
        later `USING (k)` or `WHERE k = ...` sees one column; `a.k` and `b.k`
        still read their own side."""
        bindings = list(lowered.bindings)
        merged: list[_Binding] = []
        coalesced: list[tuple[str, ir.OutputColumn, ir.OutputColumn]] = []
        for identifier in join.args.get("using") or ():
            name = identifier.name.lower()
            sides = [
                [index for index in (range(left_count) if left else range(left_count, len(bindings)))
                 if bindings[index].name == name and not bindings[index].shadowed and not bindings[index].qualifier.startswith(_SCALAR_QUALIFIER)]
                for left in (True, False)
            ]
            if any(len(side) != 1 for side in sides):
                continue  # Not one column on each side: leave the names as they are.
            (mine,), (theirs,) = sides
            for index in (mine, theirs):
                bindings[index] = replace(bindings[index], shadowed=True)
            if kind == "full":
                coalesced.append((name, bindings[mine].column, bindings[theirs].column))
            else:
                column = bindings[theirs if kind == "right" else mine].column
                merged.append(_Binding(_USING_QUALIFIER, name, column))
        relation = lowered.relation
        if coalesced:
            passed = _pass_output_columns(relation.output_columns, origin)
            remapped = dict(zip(relation.output_columns, passed))
            values = tuple(
                ir.OutputColumn(origin=origin, name=name, scalar_expr=ir.Coalesce(origin=origin, arguments=(
                    ir.ColumnRef(origin=origin, column=remapped[first]), ir.ColumnRef(origin=origin, column=remapped[second]),
                )))
                for name, first, second in coalesced
            )
            relation = ir.Project(origin=origin, output_columns=(*passed, *values), is_schema_complete=relation.is_schema_complete, input=relation)
            bindings = [replace(binding, column=remapped[binding.column]) for binding in bindings]
            merged = [replace(binding, column=remapped[binding.column]) for binding in merged]
            merged += [_Binding(_USING_QUALIFIER, name, value) for (name, _, _), value in zip(coalesced, values)]
        return _Lowered(relation, (*bindings, *merged))

    def _opaque_join(self, left: _Lowered, right: _Lowered, join: exp.Join, origin: Origin) -> _Lowered:
        """A join whose kind is not modelled: every column it outputs is opaque,
        and a marker on it is reported as unanalyzed."""
        description = join.sql(dialect=self.dialect)
        input_columns = left.relation.output_columns + right.relation.output_columns
        opaque = {
            column: ir.OutputColumn(origin=origin, name=column.name, scalar_expr=ir.OpaqueExpression(origin=origin, description=description))
            for column in input_columns
        }
        relation = ir.OpaqueRelation(origin=origin, output_columns=tuple(opaque.values()), is_schema_complete=False, description=description)
        return _Lowered(relation, tuple(
            replace(binding, column=opaque[binding.column]) for binding in left.bindings + right.bindings
        ))

    def _join_marker(self, join: exp.Join, relation: ir.Join) -> None:
        kind = join_marker_kind(join)
        if kind is not None:
            self._handled_joins.add(id(join))
        cardinality = {"unique-join": UniqueJoin, "many-to-one-join": ManyToOneJoin, "one-to-many-join": OneToManyJoin, "one-to-one-join": OneToOneJoin}
        if kind in cardinality and relation.kind in {"semi", "anti"}:
            # It keeps or drops left rows whole and outputs no right row: cardinality does not apply.
            self._diagnostics.append(Diagnostic(
                diag.CARDINALITY_ON_FILTERING_JOIN,
                f"the {JOIN_MARKER_NOUNS[kind]} marker on line {join_origin(join, self.dialect).line} is on a {relation.kind} join, "
                "which outputs no right rows: join cardinality does not apply to it",
                join_origin(join, self.dialect),
            ))
            return
        match kind:
            case "unique-join" | "many-to-one-join" | "one-to-many-join" | "one-to-one-join":
                self._assertions.append(ir.Assertion(origin=join_origin(join, self.dialect), property=cardinality[kind](join=relation)))
            case "exactly-one-join":
                self._assertions.append(
                    ir.Assertion(origin=join_origin(join, self.dialect), property=ExactlyOneJoin(join=relation))
                )
            case None:
                pass
            case _:
                assert_never(kind)

    def _lower_join_predicate(self, join: exp.Join, left: _Lowered, right: _Lowered) -> ir.ScalarExpr | None:
        """The ON condition; USING is an AND of one equality per column."""
        using = join.args.get("using")
        if using:
            return _and(tuple(
                ir.Equality(
                    origin=self._origin(identifier, _source_line(identifier)),
                    left=self._lower_expression(
                        exp.column(identifier.name), left.bindings, schema_complete=left.relation.is_schema_complete, line=_source_line(identifier),
                    ),
                    right=self._lower_expression(
                        exp.column(identifier.name), right.bindings, schema_complete=right.relation.is_schema_complete, line=_source_line(identifier),
                    ),
                )
                for identifier in using
            ), self._origin(join))
        predicate = join.args.get("on")
        if predicate is None:
            return None
        complete = left.relation.is_schema_complete and right.relation.is_schema_complete
        return self._lower_condition(predicate, _conjuncts(predicate), left.bindings + right.bindings, schema_complete=complete)

    def _lower_condition(
        self, source: Expr, conjuncts: list[Expr], bindings: tuple[_Binding, ...], *, schema_complete: bool
    ) -> ir.ScalarExpr:
        """The AND of `conjuncts`, the top-level conjuncts of `source`."""
        assert conjuncts, "a condition has at least one conjunct"
        lowered = _and(
            tuple(self._lower_conjunct(conjunct, bindings, schema_complete=schema_complete) for conjunct in conjuncts),
            self._origin(source, _source_line(source)),
        )
        assert lowered is not None
        return lowered

    def _lower_conjunct(self, conjunct: Expr, bindings: tuple[_Binding, ...], *, schema_complete: bool) -> ir.ScalarExpr:
        return self._lower_expression(conjunct, bindings, schema_complete=schema_complete, line=_source_line(conjunct))

    def _lower_select_tail(self, lowered: _Lowered, select: exp.Select) -> ir.RelationExpr:
        if any(
            isinstance(star, exp.Star) and any(star.args.values())
            for item in select.expressions for star in (item.this if isinstance(item, exp.Column) else item,)
        ):
            opaque = self._opaque_relation(select)
            self._unsupported.append((opaque, "a modified star projection"))
            return opaque
        keys = grouping(select)
        calls = [call for item in select.expressions for call in _set_returning_calls(item, self.dialect)]
        if calls:
            if keys is not None or isinstance(select.args.get("qualify"), exp.Qualify) or (
                select.args.get("distinct") is not None and select.args["distinct"].args.get("on") is not None
            ):
                # Evaluated after grouping or a window: not modelled.
                opaque = self._opaque_relation(select, _selected_output_names(select))
                self._unsupported.append((opaque, "a set-returning function beside GROUP BY, QUALIFY or DISTINCT ON"))
                return opaque
            lowered = self._fan_out(lowered, calls, self._origin(select))
        if keys is not None:
            relation = self._qualified(self._lower_aggregate(lowered, select, keys), select, grouped=True)
        elif select.args.get("distinct") is not None and select.args["distinct"].args.get("on") is not None:
            relation = self._lower_distinct_on(lowered, select, select.args["distinct"].args["on"])
        elif select.args.get("distinct") is not None:
            relation = self._qualified(self._lower_distinct(lowered, select), select, grouped=False)
        elif isinstance(select.args.get("qualify"), exp.Qualify):
            relation = self._lower_qualify(lowered, select, select.args["qualify"])
        else:
            if select.args.get("where") is not None:
                lowered = self._filter(lowered, select.args["where"])
            if _star_only(select):
                relation = lowered.relation
            else:
                lowered, _ = self._join_scalar_subqueries(lowered, select.expressions)
                relation = self._project(lowered, select.expressions, self._origin(select))
        if relation is None:
            opaque = self._opaque_relation(select, _selected_output_names(select))
            construct = _unsupported_grouping(select)
            if construct is not None:
                self._unsupported.append((opaque, construct))
            return opaque
        self._sort_keys[id(select)] = self._resolve_order(select, lowered.bindings, relation)
        return relation

    def _resolve_order(
        self, select: exp.Select, bindings: tuple[_Binding, ...], relation: ir.RelationExpr
    ) -> tuple[ir.OutputColumn, ...] | None:
        """Each ORDER BY key as a column of the stage it sorts (see
        `Program.sort_keys`): a selected column by its name or position, else
        the column a name binds, qualifier and schema included."""
        order = select.args.get("order")
        if order is None:
            return ()
        distinct = select.args.get("distinct")
        if distinct is not None and distinct.args.get("on") is not None:
            return None  # Its ORDER BY picks the row each group keeps.
        scope = relation.input if isinstance(relation, ir.Project) else relation
        outputs = relation.output_columns
        items = [item for item in select.expressions if not isinstance(item, exp.Star)]
        keys: list[ir.OutputColumn] = []
        for ordered in order.expressions:
            expression = ordered.this if isinstance(ordered, exp.Ordered) else ordered
            key: ir.OutputColumn | None = None
            match expression:
                case exp.Column(table="") if len(items) == len(outputs) and [item.alias_or_name.lower() for item in items].count(expression.name.lower()) == 1:
                    names = [item.alias_or_name.lower() for item in items]
                    key = _in_scope(outputs[names.index(expression.name.lower())], scope)
                case exp.Literal(is_int=True) if len(items) == len(outputs) and 1 <= int(expression.this) <= len(outputs):
                    key = _in_scope(outputs[int(expression.this) - 1], scope)
                case exp.Column():
                    schema, name = _schema_path(expression), expression.name.lower()
                    bound = [
                        binding for binding in bindings
                        if binding.name == name and (binding.qualifier == expression.table.lower() if expression.table else not binding.shadowed)
                        and (not schema or binding.schema == schema)
                    ]
                    if len(bound) == 1:
                        key = _carrying(bound[0].column, scope)
                case _:
                    pass
            if key is None:
                return None
            keys.append(key)
        return tuple(keys)

    def _fan_out(self, lowered: _Lowered, calls: list[Expr], origin: Origin) -> _Lowered:
        """A select list's set-returning calls repeat each input row once per
        value they return (none for none; several are zipped, padded with
        NULL): a cross join with an opaque relation of their values. The rows
        keep no key, the input's other columns keep their facts, and each
        call's value is that relation's column, no function of its arguments."""
        names = tuple(f"value{index}" for index in range(len(calls)))
        values = ir.OpaqueRelation(
            origin=origin, output_columns=self._opaque_output_columns(names, origin), is_schema_complete=True,
            description="rows of " + ", ".join(call.sql(dialect=self.dialect) for call in calls),
        )
        input_columns = lowered.relation.output_columns + values.output_columns
        output_columns = _pass_output_columns(input_columns, origin)
        remapped = dict(zip(input_columns, output_columns))
        join = ir.Join(
            origin=origin, output_columns=output_columns, is_schema_complete=lowered.relation.is_schema_complete,
            kind="cross", left=lowered.relation, right=values, predicate=None,
        )
        bindings = [replace(binding, column=remapped[binding.column]) for binding in lowered.bindings]
        for call, column in zip(calls, values.output_columns):
            qualifier = f"{_SET_RETURNING_QUALIFIER}{len(self._set_returning_values)}"
            self._set_returning_values[id(call)] = qualifier
            bindings.append(_Binding(qualifier, _SET_RETURNING_QUALIFIER, remapped[column]))
        return _Lowered(join, tuple(bindings))

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
        before = lowered.relation.output_columns
        lowered, carried = self._join_scalar_subqueries(lowered, remaining)
        origin = self._origin(where)
        output_columns = _pass_output_columns(lowered.relation.output_columns, origin)
        remapped = {source: output for source, output in zip(lowered.relation.output_columns, output_columns)}
        complete = lowered.relation.is_schema_complete
        relation = ir.Filter(
            origin=origin, output_columns=output_columns, is_schema_complete=complete, input=lowered.relation,
            predicate=self._lower_condition(where.this, remaining, lowered.bindings, schema_complete=complete),
        )
        bindings = tuple(
            replace(binding, column=remapped[binding.column]) for binding in lowered.bindings
        )
        if all(carried[column] is column for column in before):
            return _Lowered(relation, bindings)
        # Scalar subqueries were joined in: keep only the columns from before,
        # so their values stay inside this WHERE.
        kept = tuple(remapped[carried[column]] for column in before)
        project = ir.Project(
            origin=origin, output_columns=_pass_output_columns(kept, origin), is_schema_complete=complete, input=relation,
        )
        projected = dict(zip(kept, project.output_columns))
        return _Lowered(project, tuple(
            replace(binding, column=projected[binding.column])
            for binding in bindings if binding.column in projected
        ))

    def _correlated(self, select: exp.Select, inner: _Lowered, outer: _Lowered) -> bool | None:
        """Whether `select` reads the outer scope, only in its WHERE; None when
        it reads it anywhere else, or when a column may belong to either scope."""
        where = select.args.get("where")
        correlated = False
        for column in select.find_all(exp.Column):
            found = self._scope_of(column, inner, outer)
            if found is None:
                return None
            scope, binding = found
            self._scoped_columns[id(column)] = binding.column
            if scope == "outer":
                if where is None or column.find_ancestor(exp.Where) is not where:
                    return None  # Only a WHERE becomes the dependent join's ON.
                correlated = True
        return correlated

    def _scope_of(
        self, column: exp.Column, inner: _Lowered, outer: _Lowered
    ) -> tuple[Literal["inner", "outer"], _Binding] | None:
        """The scope a subquery's column belongs to, the innermost that has it,
        and its binding there. None when that is unknown (a table with no
        CREATE may have the column) or the name binds more than one column."""
        name = column.name.lower()

        def one(scope: Literal["inner", "outer"], bindings: list[_Binding]) -> tuple[Literal["inner", "outer"], _Binding] | None:
            return (scope, bindings[0]) if len(bindings) == 1 else None

        if column.table:
            qualifier, schema = column.table.lower(), _schema_path(column)
            for scope, bindings in (("inner", inner.bindings), ("outer", outer.bindings)):
                if any(binding.qualifier == qualifier and (not schema or binding.schema == schema) for binding in bindings):
                    return one(scope, [
                        binding for binding in bindings
                        if binding.qualifier == qualifier and (not schema or binding.schema == schema) and binding.name == name
                    ])
            return None
        inner_named = [binding for binding in inner.bindings if binding.name == name and not binding.shadowed and self._owns(binding) != "no"]
        outer_named = [binding for binding in outer.bindings if binding.name == name and not binding.shadowed and self._owns(binding) != "no"]
        owners = {self._owns(binding) for binding in inner_named}
        if "yes" in owners:
            return one("inner", inner_named)
        outer_has = bool(outer_named)
        if "unknown" in owners or (not inner_named and not inner.relation.is_schema_complete and not any(binding.name == name for binding in inner.bindings)):
            if not outer_has:
                return one("inner", inner_named)
            self._warnings.append(Diagnostic(
                diag.AMBIGUOUS_COLUMN,
                f"column {quoted(column.name)} in a subquery may belong to its own FROM or to the outer query, "
                "since some of its tables have no declared columns: qualify it, declare those tables with CREATE TABLE, "
                "or analyze with a database connection (analyze_database)",
                self._origin(column, _source_line(column)),
            ))
            return None
        return one("outer", outer_named) if outer_has else None

    def _owns(self, binding: _Binding) -> Literal["yes", "no", "unknown"]:
        """Whether the relation behind `binding` has its column: a declared or
        computed column it has, one its declared table lacks it does not, and
        one made up for a table with no CREATE it may have."""
        base = binding.column
        while isinstance(base.scalar_expr, ir.ColumnRef):
            base = base.scalar_expr.column
        if base in self._absent:
            return "no"
        return "unknown" if base in self._guessed else "yes"

    def _join_scalar_subqueries(
        self, lowered: _Lowered, expressions: Sequence[Expr]
    ) -> tuple[_Lowered, dict[ir.OutputColumn, ir.OutputColumn]]:
        """Each scalar subquery in `expressions` joined onto `lowered`, and
        where each of `lowered`'s columns went."""
        carried = {column: column for column in lowered.relation.output_columns}
        for expression in expressions:
            for subquery in _scalar_subqueries(expression):
                joined = self._scalar_subquery_join(lowered, subquery)
                if joined is None:
                    continue
                lowered, remapped = joined
                carried = {column: remapped[current] for column, current in carried.items()}
        return lowered, carried

    def _scalar_subquery_join(
        self, lowered: _Lowered, subquery: exp.Subquery
    ) -> tuple[_Lowered, dict[ir.OutputColumn, ir.OutputColumn]] | None:
        """A scalar subquery with body B as a left join of `lowered` with B: no
        row makes it NULL, which is a left join's padding. More than one row
        is a runtime error, so the join is many to one, or the query fails.

        Uncorrelated, B is lowered whole under an implied LIMIT 1, sound since
        it changes the result only when the query would fail: its value is
        the same on every row. Correlated, B's WHERE becomes the ON of a
        dependent join (as for EXISTS), so only a plain select-from-where
        whose value is one of its columns is lowered. Anything else, or a
        column that may belong to either scope, stays opaque."""
        select = subquery.this
        if not isinstance(select, exp.Select) or len(select.expressions) != 1 or _star_only(select):
            return None
        source = _from_source(select)
        if source is None or select.args.get("with") or any(
            join.args.get("method") == "NATURAL" for join in _all_joins(select)
        ) or any(isinstance(node, (exp.Subquery, exp.Exists, exp.Query)) and node is not select for node in select.walk()):
            return None
        right = self._lower_source(source)
        for join in select.args.get("joins") or ():
            right = self._lower_join(right, join)
        correlated = self._correlated(select, right, lowered)
        origin = self._origin(subquery)
        if correlated is None:
            return None
        if not correlated:
            body = self._finish_select(self._lower_select_tail(right, select), select)
            right_relation: ir.RelationExpr = ir.Limit(
                origin=origin, output_columns=_pass_output_columns(body.output_columns, origin),
                is_schema_complete=body.is_schema_complete, input=body, count=1,
            )
            value = right_relation.output_columns[0]
            predicate = None
        else:
            if grouping(select) == [] and not any(select.args.get(key) for key in ("having", "qualify", "distinct", "limit", "offset", "windows")):
                return self._aggregate_subquery_join(lowered, subquery, select, right)
            if grouping(select) is not None or any(select.args.get(key) for key in ("having", "qualify", "distinct", "limit", "offset", "windows")):
                return None
            item = select.expressions[0]
            lowered_value = self._lower_expression(
                item.this if isinstance(item, exp.Alias) else item, right.bindings, schema_complete=right.relation.is_schema_complete,
            )
            if not (isinstance(lowered_value, ir.ColumnRef) and lowered_value.column in right.relation.output_columns):
                return None
            inner = select.args.get("where")
            predicate = None if inner is None else self._lower_condition(
                inner.this, _conjuncts(inner.this), lowered.bindings + right.bindings,
                schema_complete=lowered.relation.is_schema_complete and right.relation.is_schema_complete,
            )
            right_relation, value = right.relation, lowered_value.column
        input_columns = lowered.relation.output_columns + right_relation.output_columns
        output_columns = _pass_output_columns(input_columns, origin)
        remapped = dict(zip(input_columns, output_columns))
        join = ir.Join(
            origin=origin, output_columns=output_columns,
            is_schema_complete=lowered.relation.is_schema_complete and right_relation.is_schema_complete,
            kind="left", left=lowered.relation, right=right_relation, predicate=predicate, scalar_subquery=True,
        )
        position = marker_position(select)
        if position is not None:
            self._scalar_subqueries.append((AttachmentSite("scalar subquery", position[1], position[0], True), join))
        qualifier = f"{_SCALAR_QUALIFIER}{len(self._scalar_values)}"
        self._scalar_values[id(subquery)] = qualifier
        bindings = (
            *(replace(binding, column=remapped[binding.column]) for binding in lowered.bindings),
            _Binding(qualifier, _SCALAR_QUALIFIER, remapped[value]),
        )
        return _Lowered(join, bindings), remapped

    def _aggregate_subquery_join(
        self, lowered: _Lowered, subquery: exp.Subquery, select: exp.Select, right: _Lowered,
    ) -> tuple[_Lowered, dict[ir.OutputColumn, ir.OutputColumn]] | None:
        """A correlated scalar subquery whose value is one aggregate call,
        `(SELECT agg(...) FROM r WHERE r.k = outer.k AND p)`, decorrelated:
        r filtered by p, grouped by k, left joined on `k = outer.k`. Each
        outer row meets at most its one group, and its value is a function of
        the key. A row with no group gets the aggregate over no rows: NULL,
        the join's padding, for the aggregates whose value over no rows is
        NULL (`_EMPTY_NULL_AGGREGATES`); a count's is 0 (the count bug), so its
        value is `coalesce(count, 0)`; any other aggregate is not
        decorrelated. Nor is one whose arguments read the outer row: SQL
        then makes it an aggregate of the outer query. Only equalities to outer
        columns of the same declared type correlate it: grouping by `k`
        then merges exactly the rows one outer value matches."""
        where = select.args.get("where")
        item = select.expressions[0]
        value_expression = item.this if isinstance(item, exp.Alias) else item
        if where is None or not isinstance(value_expression, _EMPTY_NULL_AGGREGATES | exp.Count):
            return None
        if any(
            found is None or found[0] != "inner"
            for found in (self._scope_of(column, right, lowered) for column in value_expression.find_all(exp.Column))
        ):
            return None
        keys: list[tuple[Expr, ir.OutputColumn, ir.OutputColumn]] = []  # (inner column, inner, outer)
        inner_only: list[Expr] = []
        for conjunct in _conjuncts(where.this):
            scoped = [self._scope_of(column, right, lowered) for column in conjunct.find_all(exp.Column)]
            if any(found is None for found in scoped):
                return None
            scopes = {found[0] for found in scoped if found is not None}
            if scopes <= {"inner"}:
                inner_only.append(conjunct)
                continue
            if not isinstance(conjunct, exp.EQ) or not isinstance(conjunct.this, exp.Column) or not isinstance(conjunct.expression, exp.Column):
                return None
            sides = [(node, self._scope_of(node, right, lowered)) for node in (conjunct.this, conjunct.expression)]
            inner = [(node, found[1].column) for node, found in sides if found is not None and found[0] == "inner"]
            outer = [found[1].column for _, found in sides if found is not None and found[0] == "outer"]
            if len(inner) != 1 or len(outer) != 1:
                return None
            if _traced_comparison_type(inner[0][1]) is None or _traced_comparison_type(inner[0][1]) != _traced_comparison_type(outer[0]):
                return None
            keys.append((inner[0][0], inner[0][1], outer[0]))
        if not keys:
            return None
        grouped = select.copy()
        grouped.set("expressions", [
            *(exp.alias_(node.copy(), f"k{index}") for index, (node, _, _) in enumerate(keys)), exp.alias_(value_expression.copy(), "value"),
        ])
        grouped.set("where", exp.Where(this=exp.and_(*(conjunct.copy() for conjunct in inner_only))) if inner_only else None)
        grouped.set("group", exp.Group(expressions=[node.copy() for node, _, _ in keys]))
        body = self._lower_select_tail(right, grouped)
        if not isinstance(body, ir.Aggregate):
            return None
        origin = self._origin(subquery)
        predicate = _and(tuple(
            ir.Equality(origin=origin, left=ir.ColumnRef(origin=origin, column=outer), right=ir.ColumnRef(origin=origin, column=body.output_columns[index]))
            for index, (_, _, outer) in enumerate(keys)
        ), origin)
        input_columns = lowered.relation.output_columns + body.output_columns
        output_columns = _pass_output_columns(input_columns, origin)
        remapped = dict(zip(input_columns, output_columns))
        join = ir.Join(
            origin=origin, output_columns=output_columns, is_schema_complete=lowered.relation.is_schema_complete,
            kind="left", left=lowered.relation, right=body, predicate=predicate, scalar_subquery=True,
        )
        position = marker_position(select)
        if position is not None:
            self._scalar_subqueries.append((AttachmentSite("scalar subquery", position[1], position[0], True), join))
        relation: ir.RelationExpr = join
        value = remapped[body.output_columns[-1]]
        if isinstance(value_expression, exp.Count):
            passed = _pass_output_columns(output_columns, origin)
            zero = ir.Constant(origin=origin, value=ir.LiteralValue(type="numeric", text="0"))
            counted = ir.OutputColumn(origin=origin, name="value", scalar_expr=ir.Coalesce(
                origin=origin, arguments=(ir.ColumnRef(origin=origin, column=value), zero),
            ))
            relation = ir.Project(origin=origin, output_columns=(*passed, counted), is_schema_complete=join.is_schema_complete, input=join)
            remapped = {source: passed[output_columns.index(output)] for source, output in remapped.items()}
            value = counted
        qualifier = f"{_SCALAR_QUALIFIER}{len(self._scalar_values)}"
        self._scalar_values[id(subquery)] = qualifier
        bindings = (
            *(replace(binding, column=remapped[binding.column]) for binding in lowered.bindings),
            _Binding(qualifier, _SCALAR_QUALIFIER, value),
        )
        return _Lowered(relation, bindings), remapped

    def _subquery_join(self, lowered: _Lowered, conjunct: Expr) -> _Lowered | None:
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
            if source is None or grouping(select) is not None or any(
                select.args.get(key) for key in ("having", "qualify", "distinct", "limit", "offset", "with", "windows")
            ) or any(join.args.get("method") == "NATURAL" for join in _all_joins(select)):
                # Grouped by nothing (`grouping` is []), it returns one row whatever its WHERE: EXISTS is always true.
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
        return _Lowered(relation, tuple(replace(binding, column=remapped[binding.column]) for binding in left.bindings))

    def _project(self, lowered: _Lowered, items: list[Expr], origin: Origin) -> ir.Project:
        output_columns = self._selected_output_columns(items, lowered)
        return ir.Project(origin=origin, output_columns=output_columns, is_schema_complete=True, input=lowered.relation)

    def _lower_aggregate(self, lowered: _Lowered, select: exp.Select, keys: list[Expr]) -> ir.RelationExpr | None:
        """`select` grouped by `keys`; [] is one group, one row however many
        rows its input has. HAVING filters the groups (`_lower_having`). A
        key not selected, or an aggregate only HAVING reads, is a hidden
        output of the Aggregate, which a Project then drops: a key still
        splits the groups, and HAVING may read either."""
        if _unsupported_grouping(select) is not None:
            return None
        if select.args.get("where") is not None:
            lowered = self._filter(lowered, select.args["where"])

        items: list[Expr] = list(select.expressions)
        selected_names = {item.alias_or_name.lower() for item in items}
        grouped_items: set[int] = set()
        matched: list[Expr] = []
        for group_expression in keys:
            item = _matching_grouping_output(group_expression, select.expressions) or _same_column_alias(group_expression, select.expressions)
            if item is None:
                if (isinstance(group_expression, exp.Literal) and group_expression.is_int) or (
                    isinstance(group_expression, exp.Column) and not group_expression.table
                    and group_expression.name.lower() in selected_names
                ) or group_expression.find(exp.AggFunc, exp.Window, exp.Subquery) is not None:
                    return None  # A position out of range, a name that may be a select alias, or not a key.
                item = exp.alias_(group_expression.copy(), f"{HIDDEN_GROUPING}{len(items)}")
                items.append(item)
            grouped_items.add(id(item))
            matched.append(item)
        having = select.args.get("having")
        if having is not None:
            # An aggregate HAVING reads that no item writes: a hidden output,
            # like a grouping key not selected, that `_lower_having` reads.
            written = {_unaliased(item).sql(dialect=self.dialect) for item in items}
            for call in _outermost_aggregate_calls(having.this):
                text = call.sql(dialect=self.dialect)
                if text not in written and self._repeatable(call):
                    written.add(text)
                    items.append(exp.alias_(call.copy(), f"{HIDDEN_AGGREGATE}{len(items)}"))

        output_columns = self._aggregate_output_columns(items, lowered, grouped_items)
        grouping_outputs = [output_columns[next(index for index, item in enumerate(items) if item is match)] for match in matched]
        calls = tuple(
            ir.AggregateCall(
                origin=self._origin(call), description=call.sql(dialect=self.dialect),
                arguments=tuple(
                    self._lower_expression(argument, lowered.bindings, schema_complete=lowered.relation.is_schema_complete)
                    for argument in _aggregate_arguments(call)
                ),
            )
            for item in select.expressions for call in item.find_all(exp.AggFunc)
            if call.find_ancestor(exp.Window, exp.Select) is select
        )
        aggregate = ir.Aggregate(
            origin=self._origin(select), output_columns=output_columns, is_schema_complete=True, input=lowered.relation,
            grouping_outputs=tuple(grouping_outputs), calls=calls,
        )
        relation: ir.RelationExpr = aggregate if having is None else self._lower_having(aggregate, items, having)
        if len(items) == len(select.expressions):
            return relation
        origin = self._origin(select)
        selected = _pass_output_columns(relation.output_columns[:len(select.expressions)], origin)
        return ir.Project(origin=origin, output_columns=selected, is_schema_complete=True, input=relation)

    def _lower_having(self, aggregate: ir.Aggregate, items: list[Expr], having: exp.Having) -> ir.Filter:
        """HAVING as a Filter of the grouped rows. A condition reads the
        aggregate's output columns, so each part of it written as a select
        item is that item's column (`count(*) > 1` beside `count(*) AS c` is
        `c > 1`), and so is each aggregate no item writes, a hidden item
        (`_lower_aggregate`). A conjunct still reading anything else (a column
        neither grouped nor aggregated, an aggregate whose repeated text may
        differ, a subquery) is opaque: it removes groups for a reason not
        known, which keeps every fact that holds of the groups, and states
        nothing of its own: no selected column can show it."""
        qualifier = "__having"
        bindings = tuple(_Binding(qualifier, f"c{index}", column) for index, column in enumerate(aggregate.output_columns))
        written: dict[str, int] = {}
        for index, item in enumerate(items):
            expression = _unaliased(item)
            if self._repeatable(expression):
                written.setdefault(expression.sql(dialect=self.dialect), index)

        def selected(node: Expr) -> Expr:
            index = written.get(node.sql(dialect=self.dialect))
            return node if index is None or isinstance(node, exp.Literal) else exp.column(f"c{index}", table=qualifier)

        origin = self._origin(having)
        parts: list[ir.ScalarExpr] = []
        for conjunct in _conjuncts(having.this):
            rewritten = conjunct.copy().transform(selected)
            unknown = any(
                column.table != qualifier for column in rewritten.find_all(exp.Column)
            ) or rewritten.find(exp.AggFunc, exp.Subquery, exp.Window) is not None
            if unknown:
                parts.append(ir.OpaqueExpression(origin=self._origin(conjunct), description=conjunct.sql(dialect=self.dialect)))
            else:
                parts.append(self._lower_expression(rewritten, bindings, schema_complete=True, line=_source_line(conjunct)))
        return ir.Filter(
            origin=origin, output_columns=_pass_output_columns(aggregate.output_columns, origin), is_schema_complete=True,
            input=aggregate, predicate=_and(tuple(parts), origin),
        )

    def _repeatable(self, expression: Expr) -> bool:
        """Whether every occurrence of `expression`'s SQL text in one grouped
        select has one value per group. Repeated text need not denote the same
        evaluation: only modelled pure expressions and aggregates over pure
        inputs qualify, not a literal (which a name may shadow)."""
        return not isinstance(expression, exp.Literal) and all(
            not isinstance(node, _NOT_A_FUNCTION_OF_ITS_COLUMNS)
            and (not isinstance(node, exp.Func) or type(node) in dialect_builtins(self.semantics))
            or isinstance(node, exp.Count | exp.Sum | exp.Min | exp.Max | exp.Avg | exp.Star)
            for node in expression.walk()
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

    def _qualified(self, relation: ir.RelationExpr | None, select: exp.Select, *, grouped: bool) -> ir.RelationExpr | None:
        """`relation`, the groups (`grouped`) or distinct rows of `select`, after its
        QUALIFY. Over groups, `row_number() OVER (PARTITION BY k...) = 1` with
        every key a selected item keeps one row per partition. Any other
        QUALIFY drops rows for a reason not known: an opaque Filter, which
        keeps every fact of the rows and none that needs them all."""
        qualify = select.args.get("qualify")
        if relation is None or not isinstance(qualify, exp.Qualify):
            return relation
        origin = self._origin(qualify)
        output_columns = _pass_output_columns(relation.output_columns, origin)
        window = _row_number_equals_one(qualify.this)
        if window is not None and grouped:  # Not over DISTINCT: its window sees the rows before it.
            positions = _partition_positions(window.args.get("partition_by") or (), select.expressions)
            if positions and len(relation.output_columns) == len(select.expressions):
                return ir.QualifyByPartition(
                    origin=origin, output_columns=output_columns, is_schema_complete=True, input=relation,
                    partition_outputs=tuple(output_columns[position] for position in positions),
                )
        return ir.Filter(
            origin=origin, output_columns=output_columns, is_schema_complete=True, input=relation,
            predicate=ir.OpaqueExpression(origin=origin, description=qualify.this.sql(dialect=self.dialect)),
        )

    def _lower_distinct_on(self, lowered: _Lowered, select: exp.Select, on: Expr) -> ir.QualifyByPartition | None:
        """`DISTINCT ON (k...)` keeps one row per distinct `k...`, like
        `QUALIFY ROW_NUMBER() OVER (PARTITION BY k...) = 1`. Its ORDER BY only
        picks which row survives, and may name outputs, so it is not lowered."""
        if select.args.get("qualify") is not None:
            return None
        keys = on.expressions if isinstance(on, exp.Tuple) else [on]
        return self._one_row_per_partition(lowered, select, keys, ())

    def _one_row_per_partition(
        self, lowered: _Lowered, select: exp.Select, partitions: Sequence[Expr], order: Sequence[exp.Ordered],
    ) -> ir.QualifyByPartition | None:
        """Every partition key must be a selected output: the Unique Set is stated on outputs."""
        if not partitions or select.args.get("having") is not None or _star_only(select):
            return None
        if select.args.get("where") is not None:
            lowered = self._filter(lowered, select.args["where"])

        positions = _partition_positions(partitions, select.expressions)
        if positions is None:
            return None
        output_columns = self._selected_output_columns(select.expressions, lowered)
        partition_outputs = [output_columns[position] for position in positions]
        ordering = tuple(
            self._lower_expression(ordered.this, lowered.bindings, schema_complete=lowered.relation.is_schema_complete)
            for ordered in order
        )
        return ir.QualifyByPartition(
            origin=self._origin(select), output_columns=output_columns, is_schema_complete=True, input=lowered.relation,
            partition_outputs=tuple(partition_outputs), ordering=ordering,
        )

    def _selected_output_columns(self, items: list[Expr], lowered: _Lowered) -> tuple[ir.OutputColumn, ...]:
        columns = []
        for item in items:
            expression = item.this if isinstance(item, exp.Alias) else item
            value = self._lower_expression(expression, lowered.bindings, schema_complete=lowered.relation.is_schema_complete)
            comparison_type = None
            if isinstance(expression, exp.Cast):
                kind = expression.to
                if kind.this not in exp.DataType.TEXT_TYPES or (
                    isinstance(value, ir.Call) and (source := self._declared_type(value.arguments[0])) is not None
                    and source.this in exp.DataType.NUMERIC_TYPES
                ):
                    comparison_type = self._comparison_type(exp.ColumnDef(kind=kind))
            column = ir.OutputColumn(origin=self._origin(item), name=item.alias_or_name, scalar_expr=value, comparison_type=comparison_type)
            if isinstance(expression, exp.Cast):
                self._declared_types[column] = expression.to
            columns.append(column)
        return tuple(columns)

    def _aggregate_output_columns(self, items: list[Expr], source: _Lowered, grouped_items: set[int]) -> tuple[ir.OutputColumn, ...]:
        """Each item of a grouped select. A column outside every aggregate must
        be one the grouping determines (`_grouping_determined`): any other
        (SQLite's bare column) makes its item opaque (ADR 0006)."""
        grouped = {
            id(item): self._lower_expression(
                item.this if isinstance(item, exp.Alias) else item, source.bindings, schema_complete=source.relation.is_schema_complete,
            )
            for item in items if id(item) in grouped_items
        }
        determined = self._grouping_determined({
            lowered.column for lowered in grouped.values() if isinstance(lowered, ir.ColumnRef)
        }, source.relation)
        # Grouped by nothing (no grouped items), the one row exists even
        # over no input rows, where an aggregate of its values is NULL.
        empty = not grouped_items
        output_columns: list[ir.OutputColumn] = []
        for item in items:
            expression = item.this if isinstance(item, exp.Alias) else item
            lowered = grouped[id(item)] if id(item) in grouped else self._lower_grouped(expression, source, determined, empty=empty)
            if id(item) not in grouped_items and _reads_ungrouped_column(lowered, determined):
                lowered = ir.OpaqueExpression(origin=self._origin(expression), description=expression.sql(dialect=self.dialect))
                self._unsupported.append((lowered, "a column neither grouped nor inside an aggregate"))
            output_columns.append(ir.OutputColumn(origin=self._origin(item), name=item.alias_or_name, scalar_expr=lowered))
        return tuple(output_columns)

    def _lower_grouped(
        self, expression: Expr, source: _Lowered, determined: Callable[[ir.OutputColumn], bool], *, empty: bool,
    ) -> ir.ScalarExpr:
        """`expression`, computed once per group of `source`'s rows: each
        aggregate call is its value (`_aggregate_value`), and each column
        outside them the grouping determines is one value per group, an
        AnyAggregate. Any other column is left as it is, for the caller to
        reject; windows and subqueries are not entered."""
        values: dict[int, ir.ScalarExpr] = {}
        pending: list[Expr] = [expression]
        while pending:
            node = pending.pop()
            if isinstance(node, exp.Window | exp.Query | exp.Subquery):
                continue
            if _is_aggregate_call(node):
                value = self._aggregate_value(node, source, empty=empty)
                if value is not None:
                    values[id(node)] = value
                continue
            if isinstance(node, exp.Column):
                column = self._lower_expression(node, source.bindings, schema_complete=source.relation.is_schema_complete)
                if isinstance(column, ir.ColumnRef) and determined(column.column):
                    values[id(node)] = ir.AnyAggregate(origin=column.origin, input=column, empty=empty)
                continue
            pending.extend(node.iter_expressions())
        self._grouped_values.update(values)
        try:
            return self._lower_expression(expression, source.bindings, schema_complete=source.relation.is_schema_complete)
        finally:
            for key in values:
                del self._grouped_values[key]

    def _aggregate_value(self, call: Expr, source: _Lowered, *, empty: bool) -> ir.ScalarExpr | None:
        """The value of an aggregate `call` over a group of `source`'s rows,
        or None when it is not modelled. `min`, `max` and `any_value` pick a
        value of their argument (AnyAggregate), `sum` and `avg` accumulate
        its non-null values where that is never NULL (Accumulation,
        `checked_sum`), and `count` is never NULL. One with FILTER may
        see none of the group's rows: NULL, but for count."""
        if isinstance(call, exp.Filter):
            call, empty = call.this, True
        origin = self._origin(call)

        def lower(argument: Expr) -> ir.ScalarExpr:
            return self._lower_expression(argument, source.bindings, schema_complete=source.relation.is_schema_complete)

        if isinstance(call, exp.Count):
            return ir.Count(origin=origin, description=call.sql(dialect=self.dialect))
        if (argument := _picked_argument(call)) is not None:
            # One of its argument's values on some row of the group.
            return ir.AnyAggregate(origin=origin, input=lower(argument), empty=empty)
        if (argument := _aggregate_argument(call, (exp.Sum, exp.Avg), distinct=True)) is not None and self.semantics.checked_sum:
            # Elsewhere an overflow or NaN over non-null values may be NULL.
            return ir.Accumulation(origin=origin, input=lower(argument), empty=empty)
        return None

    def _grouping_determined(self, grouping: set[ir.OutputColumn], relation: ir.RelationExpr) -> Callable[[ir.OutputColumn], bool]:
        """Whether its `grouping` columns determine a column of `relation`: one of those,
        and every column of a table occurrence whose primary key, declared in
        the SQL, they include (SQL's functional dependence on a key, which
        PostgreSQL and MySQL accept)."""
        occurrences: dict[ir.OutputColumn, ir.Alias] = {}
        pending: list[ir.RelationExpr] = [relation]
        while pending:
            node = pending.pop()
            if isinstance(node, ir.Alias):
                occurrences.update((output, node) for output in node.output_columns)
            else:
                pending.extend(child for child in ir.children(node) if isinstance(child, ir.RelationExpr))

        def occurrence(column: ir.OutputColumn) -> tuple[ir.Alias | None, ir.OutputColumn]:
            while column not in occurrences and isinstance(column.scalar_expr, ir.ColumnRef):
                column = column.scalar_expr.column
            return occurrences.get(column), column

        keyed: set[ir.Alias] = set()
        grouped_names: dict[ir.Alias, set[str]] = {}
        for column in grouping:
            alias, output = occurrence(column)
            if alias is not None:
                grouped_names.setdefault(alias, set()).add(output.name.lower())
        for alias, names in grouped_names.items():
            symbol = next((symbol for symbol, named in self._named_relations.items() if named is alias.source), None)
            key = None if symbol is None or symbol.role is not ir.RelationRole.TABLE or symbol.statement is None else _primary_key(symbol)
            if key and {name.lower() for name in key} <= names:
                keyed.add(alias)
        return lambda column: column in grouping or occurrence(column)[0] in keyed

    def _lower_expression(
        self, expression: Expr, bindings: tuple[_Binding, ...], *, schema_complete: bool = False, line: int | None = None
    ) -> ir.ScalarExpr:
        """An unqualified column resolves by name across several sources only
        when every source's schema is complete: otherwise the one visible match
        might be shadowed by a column of a source whose columns are unknown.

        A Builtin becomes a Call; any other function or operator an
        OpaquePureFunction of its lowered arguments, unless its value is not a
        function of them (`_NOT_A_FUNCTION_OF_ITS_COLUMNS`)."""
        origin = self._origin(expression, line)
        if (grouped := self._grouped_values.get(id(expression))) is not None:
            return grouped

        def lower(node: Expr) -> ir.ScalarExpr:
            return self._lower_expression(node, bindings, schema_complete=schema_complete)

        if isinstance(expression, exp.Subquery) and id(expression) in self._scalar_values:
            qualifier = self._scalar_values[id(expression)]
            value = next((binding.column for binding in bindings if binding.qualifier == qualifier), None)
            if value is not None:
                return ir.ColumnRef(origin=origin, column=value)
        if id(expression) in self._set_returning_values:
            qualifier = self._set_returning_values[id(expression)]
            value = next((binding.column for binding in bindings if binding.qualifier == qualifier), None)
            if value is not None:
                return ir.ColumnRef(origin=origin, column=value)
        if isinstance(expression, exp.Column) and id(expression) in self._scoped_columns:
            column = self._scoped_columns[id(expression)]
            if any(binding.column is column for binding in bindings):
                return ir.ColumnRef(origin=origin, column=column)
        if isinstance(expression, exp.Column):
            if expression.table:
                schema = _schema_path(expression)
                matches = [
                    binding for binding in bindings
                    if binding.qualifier == expression.table.lower() and binding.name == expression.name.lower() and (not schema or binding.schema == schema)
                ]
            elif schema_complete or len({
                binding.qualifier for binding in bindings if not binding.qualifier.startswith((_SCALAR_QUALIFIER, _USING_QUALIFIER))
            }) == 1:
                matches = [binding for binding in bindings if binding.name == expression.name.lower() and not binding.shadowed]
            else:
                matches = []
                sources = ", ".join(dict.fromkeys(binding.qualifier for binding in bindings))
                self._warnings.append(Diagnostic(
                    diag.AMBIGUOUS_COLUMN,
                    f"column {quoted(expression.name)} is unqualified and more than one source may own it ({sources}), "
                    "since some have no declared columns: qualify it, declare those tables with CREATE TABLE, "
                    "or analyze with a database connection (analyze_database)",
                    origin if origin.line is not None else replace(origin, line=_source_line(expression)),
                ))
            if len(matches) == 1:
                return ir.ColumnRef(origin=origin, column=matches[0].column)
            return ir.OpaqueExpression(origin=origin, description=expression.sql(dialect=self.dialect))
        if isinstance(expression, _COMPARISONS):
            self._check_zoned_text(expression, lower, self._origin(expression, line or _source_line(expression)))
        description = expression.sql(dialect=self.dialect)
        quantified = isinstance(expression, exp.Binary) and any(
            isinstance(operand, (exp.Any, exp.All)) for operand in (expression.this, expression.expression)
        )  # `n > ANY (...)` is FALSE over no rows, even for a NULL `n`.
        match expression:
            case exp.Paren():
                return self._lower_expression(expression.this, bindings, schema_complete=schema_complete, line=line)
            case exp.Collate():
                opaque = ir.OpaqueExpression(origin=origin, description=description)
                self._unsupported.append((opaque, "COLLATE"))
                return opaque
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
                return ir.Equality(
                    origin=origin, left=lower(expression.this), right=lower(expression.expression),
                    total=self._checked_comparison(expression, lower),
                )
            case exp.NullSafeEQ() if not quantified:
                return ir.NullSafeEquality(
                    origin=origin, left=lower(expression.this), right=lower(expression.expression),
                    rejects=self._checked_comparison(expression, lower),
                )
            case exp.NullSafeNEQ() if not quantified:
                return ir.Call(origin=origin, function=_IS_DISTINCT_FROM, arguments=(lower(expression.this), lower(expression.expression)), description=description)
            case exp.EQ() | exp.NEQ() | exp.GT() | exp.GTE() | exp.LT() | exp.LTE() if isinstance(expression.expression, exp.Any) and not isinstance(expression.this, (exp.Any, exp.All)):
                function = _any_comparison(type(expression))
                arguments = (lower(expression.this), lower(expression.expression))
                return ir.Call(origin=origin, function=function, arguments=arguments, description=description)
            case exp.Exists():
                subquery = ir.OpaqueExpression(origin=origin, description=description)
                return ir.Call(origin=origin, function=_EXISTS, arguments=(subquery,), description=description)
            case exp.Is(expression=exp.Null()):
                return ir.IsNull(origin=origin, operand=lower(expression.this))
            case exp.Is(expression=exp.Boolean() as truth):
                function = _IS_TRUE if truth.this else _IS_FALSE
                return ir.Call(origin=origin, function=function, arguments=(lower(expression.this),), description=description)
            case exp.Not(this=exp.Is(expression=exp.Null()) as test):
                return ir.IsNotNull(origin=origin, operand=lower(test.this))
            case exp.In() if expression.expressions and not any(expression.args.get(key) for key in ("query", "unnest", "field")):
                return ir.InList(origin=origin, operand=lower(expression.this), values=tuple(lower(value) for value in expression.expressions))
            case _:
                pass
        value = literal_value(expression) or _cast_integer_literal(expression)
        if value == ir.LiteralValue(type="string", text="") and self.semantics.empty_string_is_null:
            return ir.NullLiteral(origin=origin)
        if value is not None or isinstance(expression, exp.Literal):
            return ir.Constant(origin=origin, value=value)
        builtin = dialect_builtins(self.semantics).get(type(expression))
        if builtin is not None and not quantified:
            function, keys = builtin
            if type(expression) is exp.Cast and function.total is not None and self._is_json(expression.this, lower):
                # `CAST(JSON 'null' AS BIGINT)` is NULL.
                function = replace(function, total=None)
            values = [value for key in keys for value in _as_list(expression.args[key])]
            # `trim(x, chars)` and `floor(x, 2)` read more than their Builtin's arguments.
            extra = type(expression) in _ONE_ARGUMENT_BUILTINS and any(
                value for key, value in expression.args.items() if key not in keys
            )
            if function.accepts(len(values)) and not extra:
                arguments = tuple(lower(value) for value in values)
                composites = [
                    (value, argument) for value, argument in zip(values, arguments)
                    if function.name in _ORDER_COMPARISONS and function.total is not None and self._is_composite(value, lower)
                ]
                if composites:
                    function, arguments = self._composite_comparison(expression, function, arguments, composites, self._origin(expression, line or _source_line(expression)))
                return ir.Call(origin=origin, function=function, arguments=arguments, description=description)
        if isinstance(expression, _NOT_A_FUNCTION_OF_ITS_COLUMNS):
            opaque = ir.OpaqueExpression(origin=origin, description=description)
            if isinstance(expression, exp.Window):
                self._unsupported.append((opaque, "a window function (other than QUALIFY row_number() ... = 1)"))
            return opaque
        return ir.OpaquePureFunction(origin=origin, arguments=tuple(lower(child) for child in expression.iter_expressions()), description=description)

    def _checked_comparison(self, comparison: exp.Binary, lower: Callable[[Expr], ir.ScalarExpr]) -> bool:
        """Whether `comparison` compares its operands' values as they are:
        where a failed implicit cast raises, and neither is a composite, which
        may hold a NULL element."""
        return self.semantics.casts_raise and not any(
            self._is_composite(operand, lower) for operand in (comparison.this, comparison.expression)
        )

    def _check_zoned_text(self, comparison: Expr, lower: Callable[[Expr], ir.ScalarExpr], origin: Origin) -> None:
        """Warn of a comparison with a CAST of a zoned timestamp, where it keeps
        its zone: equal instants cast to different text (or dates, or local
        times), so the comparison depends on the zone each value was given."""
        if self.semantics.zoned_timestamp_is_instant or id(comparison) in self._zone_checked:
            return
        self._zone_checked.add(id(comparison))
        operands = [comparison.this, *_as_list(comparison.args.get("expression") or []), *comparison.expressions]
        operands += [comparison.args[key] for key in ("low", "high") if comparison.args.get(key) is not None]
        for operand in operands:
            while isinstance(operand, exp.Paren):
                operand = operand.this
            if not isinstance(operand, exp.Cast) or operand.to.this in _ZONED_TYPES or not isinstance(operand.this, exp.Column):
                continue
            kind = self._declared_type(lower(operand.this))
            if kind is None or kind.this not in _ZONED_TYPES:
                continue
            cast = operand.sql(dialect=self.dialect)
            self._warnings.append(Diagnostic(
                diag.ZONED_TIMESTAMP_CAST,
                f"{cast} is compared, but a {kind.sql(dialect=self.dialect)} keeps the zone it was given in {self.dialect}, "
                "so equal instants cast to different values and the comparison depends on each value's zone: "
                "compare the timestamps themselves, or convert to one zone first (AT TIME ZONE 'UTC')",
                origin,
            ))

    def _composite_comparison(
        self, comparison: Expr, function: ir.Builtin, arguments: tuple[ir.ScalarExpr, ...],
        composites: list[tuple[Expr, ir.ScalarExpr]], origin: Origin,
    ) -> tuple[ir.Builtin, tuple[ir.ScalarExpr, ...]]:
        """A comparison of composites (ARRAYs, ROWs, MAPs), non-null only when
        no element is NULL either: `ARRAY[1, NULL] <> ARRAY[1, NULL]` is NULL in
        Trino. So it also takes each composite operand's Elements, as a
        non-strict, total argument. A composite column or CAST, whose elements
        the SQL does not show, is noted for a warning."""
        assert function.total is not None
        extra = len(composites)
        widened = ir.Builtin(
            name=function.name, strict=function.strict + (False,) * extra, total=function.total + (True,) * extra, injective=False,
        )
        elements = tuple(ir.Elements(origin=argument.origin, operand=argument) for _, argument in composites)
        for value, argument in composites:
            while isinstance(value, exp.Paren):
                value = value.this
            if isinstance(value, (exp.Column, exp.Cast)) and id(value) not in self._composite_comparisons:
                self._composite_comparisons[id(value)] = (origin, comparison.sql(dialect=self.dialect), value.sql(dialect=self.dialect), argument)
        return widened, arguments + elements

    def _composite_warnings(self) -> list[Diagnostic]:
        """A warning for each composite operand whose elements no NOT NULL
        ELEMENTS declaration covers, on it or on a column it passes on from."""
        declared = {column for item in self._declarations if isinstance(item, NonNullElements) for column in item.columns}
        warnings = []
        for origin, comparison, operand, argument in self._composite_comparisons.values():
            expression, covered = argument, False
            while isinstance(expression, ir.ColumnRef) and not covered:
                covered = expression.column in declared
                expression = expression.column.scalar_expr
            if not covered:
                warnings.append(Diagnostic(
                    diag.COMPOSITE_COMPARISON,
                    f"{comparison} compares {operand}, which may hold a NULL element, and a comparison of composites with "
                    "a NULL element may be NULL (it is in Trino), so it is not taken to be non-null: if its elements are "
                    "never NULL, say so with /**DECLARE NOT NULL ELEMENTS(...)**/ on a Select Expression that outputs it",
                    origin,
                ))
        return warnings

    def _is_composite(self, node: Expr, lower: Callable[[Expr], ir.ScalarExpr]) -> bool:
        """Whether the operand may be composite, including unknown column types."""
        match node:
            case exp.Paren() | exp.Nullif():
                return self._is_composite(node.this, lower)
            case exp.Cast():
                return node.to.this in _COMPOSITE_TYPES
            case exp.Column():
                kind = self._declared_type(lower(node))
                return self.dialect in {"trino", "postgres"} if kind is None else kind.this in _COMPOSITE_TYPES
            case exp.Array() | exp.Struct() | exp.Map() | exp.VarMap():
                return True
            case exp.Coalesce():
                return any(self._is_composite(each, lower) for each in (node.this, *node.expressions))
            case exp.Case():
                arms = [arm.args["true"] for arm in node.args.get("ifs") or ()]
                default = node.args.get("default")
                return any(self._is_composite(each, lower) for each in (*arms, *([default] if default is not None else [])))
            case exp.If():
                false = node.args.get("false")
                return any(self._is_composite(each, lower) for each in (node.args["true"], *([false] if false is not None else [])))
            case _:
                return False

    def _is_json(self, node: Expr, lower: Callable[[Expr], ir.ScalarExpr]) -> bool:
        """Whether the operand may be JSON or VARIANT, whose null can cast to SQL NULL."""
        match node:
            case exp.Paren() | exp.Nullif():
                return self._is_json(node.this, lower)
            case exp.Cast():
                return node.to.this in _JSON_TYPES
            case exp.Column():
                kind = self._declared_type(lower(node))
                return kind is None or kind.this in _JSON_TYPES
            case exp.ParseJSON() | exp.JSONExtract() | exp.JSONBExtract() | exp.JSONObject() | exp.JSONArray() | exp.JSONObjectAgg() | exp.JSONArrayAgg():
                return True
            case exp.Coalesce():
                return any(self._is_json(each, lower) for each in (node.this, *node.expressions))
            case exp.Case():
                arms = [arm.args["true"] for arm in node.args.get("ifs") or ()]
                default = node.args.get("default")
                return any(self._is_json(each, lower) for each in (*arms, *([default] if default is not None else [])))
            case exp.If():
                false = node.args.get("false")
                return any(self._is_json(each, lower) for each in (node.args["true"], *([false] if false is not None else [])))
            case _:
                return False

    def _declared_type(self, expression: ir.ScalarExpr) -> exp.DataType | None:
        """The type a CREATE TABLE declares for the column `expression` reads,
        through every column that passes it on."""
        while isinstance(expression, ir.ColumnRef):
            column = expression.column
            if column in self._declared_types:
                return self._declared_types[column]
            expression = column.scalar_expr
        return None

    def _lower_case(self, case: exp.Case, bindings: tuple[_Binding, ...], *, schema_complete: bool) -> ir.SimpleCase:
        """A simple CASE, and the assertion its marker states, once per CASE."""

        def lower(expression: Expr) -> ir.ScalarExpr:
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
        span = exhaustive_span(case)
        if span is not None and id(case) not in self._handled_cases:
            self._handled_cases.add(id(case))
            origin = Origin.marked(case.sql(dialect=self.dialect), span)
            self._assertions.append(ir.Assertion(origin=origin, property=ExhaustiveCase(case=lowered)))
        return lowered

    def _opaque_relation(self, source: Expr, names=()) -> ir.OpaqueRelation:
        origin = self._origin(source)
        return ir.OpaqueRelation(
            origin=origin,
            output_columns=self._opaque_output_columns(names, origin),
            is_schema_complete=False,
            description=source.sql(dialect=self.dialect),
        )

    # Assertions and diagnostics --------------------------------------------

    def _register_column_markers(self, relation: ir.RelationExpr, select: exp.Query) -> None:
        by_name = {output.name.lower(): output for output in relation.output_columns}
        ambiguous = {name for name, count in Counter(output.name.lower() for output in relation.output_columns).items() if count > 1}
        for marker in column_markers(select):
            kind, columns, line = marker.kind, marker.columns, marker.span.line
            self._handled_column_markers.add(id(marker))
            origin = Origin.marked(select.sql(dialect=self.dialect), marker.span)
            duplicates = [column for column in (*columns, *marker.dependent) if column.lower() in ambiguous]
            if duplicates:
                self._diagnostics.append(Diagnostic(
                    diag.UNRESOLVED_ASSERTED_COLUMN, f"output names {quoted_all(duplicates)} are ambiguous; use distinct aliases", origin,
                ))
                continue
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
            members = frozenset(by_name[column.lower()] for column in columns)
            dependent = frozenset(by_name[column.lower()] for column in marker.dependent)
            match kind:
                case "foreign-key" | "declared-foreign-key":
                    self._marker_references.append(_MarkerReference(
                        marker, kind, origin, tuple(by_name[column.lower()] for column in columns),
                        self._marker_targets[id(marker)], len(self._assertions),
                    ))
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
                case "not-null-elements":
                    self._assertions.append(ir.Assertion(origin=origin, property=NonNullElements(columns=members)))
                case "declared-not-null-elements":
                    # One per column, so each column's elements are known on their own.
                    self._declarations.extend(NonNullElements(columns=frozenset({column})) for column in members)
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
                    assert_never(kind)

    def _add_attachment_site(
        self, node: exp.Join | exp.Query | exp.Case, label: str, relation: ir.RelationExpr | ir.SimpleCase, *, trailing: bool
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
                    f"the {MARKER_NOUNS[marker.kind]} on line {marker.span.line} references "
                    f"{target.name}({quoted_all(marker.referenced_columns)}), but {target.name} has no column "
                    f"{quoted_all(_missing(target, marker.referenced_columns))}",
                    pending.origin,
                ))
                continue
            match pending.kind:
                case "foreign-key":
                    placed.append((pending.position, ir.Assertion(
                        origin=pending.origin, property=ForeignKey(columns=pending.columns, referenced_columns=referenced),
                    )))
                case "declared-foreign-key":
                    self._declarations.append(ReferentialCoverage(columns=pending.columns, referenced_columns=referenced))
                case _:
                    assert_never(pending.kind)
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
                    f"{target.name}: ({quoted_all(reference.columns)}) references ({quoted_all(target_names)})",
                    reference.origin,
                ))
                continue
            referenced = self._columns_named(target, target_names)
            if referenced is None:
                self._diagnostics.append(Diagnostic(
                    diag.UNKNOWN_REFERENCED_COLUMN,
                    f"a foreign key of {table.name} references {target.name}({quoted_all(target_names)}), "
                    f"but {target.name} has no column {quoted_all(_missing(target, target_names))}",
                    reference.origin,
                ))
                continue
            trust = self._key_trust(reference.node, reference.table, reference.columns)
            if trust is not None:
                (self._knowledge if trust == "enforced" else self._declarations).append(
                    ReferentialCoverage(columns=columns, referenced_columns=referenced)
                )

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
            origin, kind = join_origin(join, self.dialect), join_marker_kind(join)
            if id(join) in self._handled_joins:
                continue
            assert kind is not None, "every asserted join records its marker kind"
            self._diagnostics.append(
                Diagnostic(
                    diag.UNANALYZED_ASSERTION,
                    f"the {JOIN_MARKER_NOUNS[kind]} assertion on line {origin.line} "
                    "is in a part of the program this analysis does not model",
                    origin,
                )
            )
        for case in _asserted_cases(ast):
            if id(case) in self._handled_cases:
                continue
            span = exhaustive_span(case)
            assert span is not None, "every asserted case records its marker span"
            self._diagnostics.append(
                Diagnostic(
                    diag.UNANALYZED_ASSERTION,
                    f"the exhaustive assertion on line {span.line} is in a part of the program this analysis does not model",
                    Origin.marked(case.sql(dialect=self.dialect), span),
                )
            )
        for select in _asserted_selects(ast):
            for marker in column_markers(select):
                if id(marker) in self._handled_column_markers:
                    continue
                self._diagnostics.append(
                    Diagnostic(
                        diag.UNANALYZED_ASSERTION,
                        f"the {MARKER_NOUNS[marker.kind]} on line {marker.span.line} is in a part of the program this analysis does not model",
                        Origin.marked(select.sql(dialect=self.dialect), marker.span),
                    )
                )

    def _table_knowledge(self, relation: ir.NamedRelation, symbol: _Symbol) -> None:
        """A declared table's ENUM types, NOT NULL columns, keys and CHECK
        domains: Knowledge where the dialect enforces them, Property
        Declarations where they are trusted (`_key_trust`)."""
        statement = symbol.statement
        if statement is None:
            return
        columns = {column.name.lower(): column for column in relation.output_columns}
        non_null = {column.lower() for column in symbol.not_null}
        # Each key's columns, its constraint, and whether it is a primary key.
        keys: list[tuple[tuple[str, ...], Expr, bool]] = []
        checks: list[exp.CheckColumnConstraint] = []
        for definition in statement.this.find_all(exp.ColumnDef):
            domain = _enum_type_values(definition, self.semantics)
            if domain is not None and definition.name.lower() in columns:
                self._knowledge.append(EnumeratedDomain(column=columns[definition.name.lower()], values=domain))
            for constraint in definition.constraints:
                match constraint.args.get("kind"):
                    case exp.NotNullColumnConstraint() as not_null if not not_null.args.get("allow_null"):
                        non_null.add(definition.name.lower())
                    case exp.PrimaryKeyColumnConstraint() as key:
                        keys.append(((definition.name,), key, True))
                    case exp.UniqueColumnConstraint() as key:
                        keys.append(((definition.name,), key, False))
                    case exp.CheckColumnConstraint() as check:
                        checks.append(check)
                    case _:
                        pass  # Other column constraints state nothing about keys or NULL.
        for constraint in _table_constraints(symbol):
            if isinstance(constraint, exp.CheckColumnConstraint):
                checks.append(constraint)
                continue
            constrained = _table_level_unique_columns(constraint)
            if constrained is not None:
                keys.append((constrained, constraint, isinstance(constraint, exp.PrimaryKey)))
        self._knowledge.extend(NonNullColumn(column=columns[name]) for name in sorted(non_null) if name in columns)
        for column_names, constraint, primary in keys:
            members = tuple(columns[name.lower()] for name in column_names if name.lower() in columns)
            if len(members) != len(column_names):
                continue
            trust = self._key_trust(constraint, symbol, column_names)
            if trust is None:
                continue
            facts: list[Property] = [UniqueSet(columns=frozenset(members))]
            if primary and (trust == "declared" or self.semantics.primary_key_not_null):
                facts.extend(NonNullColumn(column=member) for member in members if member.name.lower() not in non_null)
            (self._knowledge if trust == "enforced" else self._declarations).extend(facts)
        for parts, index in symbol.expression_keys:
            members = self._expression_key(relation, parts, index)
            if members is None:
                continue
            names = tuple(member.name for member in members)
            trust = self._key_trust(exp.UniqueColumnConstraint(), symbol, names)
            if trust is not None:
                (self._knowledge if trust == "enforced" else self._declarations).append(UniqueSet(columns=frozenset(members)))
        for check in checks:
            self._check_knowledge(check, symbol, columns)

    def _check_knowledge(self, check: exp.CheckColumnConstraint, symbol: _Symbol, columns: dict[str, ir.OutputColumn]) -> None:
        """A CHECK constraint's Enumerated Domains (`_check_domains`), or a
        warning that its shape states none sqlassert reads."""
        domains = _check_domains(check.this, self.semantics)
        if domains is None or not all(name in columns for name, _ in domains):
            assert symbol.statement is not None
            self._warnings.append(Diagnostic(
                diag.IGNORED_CHECK_CONSTRAINT,
                f"CHECK ({check.this.sql(dialect=self.dialect)}) of {symbol.name} is ignored: only col IN (...) and col = v, "
                "with literal values, joined by AND, give a column's values",
                self._origin(check, _source_line(check) or _source_line(symbol.statement)),
            ))
            return
        trust = self._key_trust(check, symbol, tuple(dict.fromkeys(name for name, _ in domains)))
        if trust is None:
            return
        facts = [EnumeratedDomain(column=columns[name], values=values) for name, values in domains]
        (self._knowledge if trust == "enforced" else self._declarations).extend(facts)

    def _expression_key(
        self, relation: ir.NamedRelation, parts: list[Expr], index: exp.Create
    ) -> tuple[ir.OutputColumn, ...] | None:
        """The columns a unique index over `parts` makes a Unique Set, or None
        (diagnosed) when it makes none.

        Two rows equal and non-null on the columns the parts read would give
        equal index keys, which the index forbids, when every part is a pure
        function of its columns (equal inputs, equal keys: so only over columns
        whose equal values are identical; under a case-insensitive collation
        `'A' = 'a'` yet their bytes differ) and total in them (a NULL key part
        exempts the row: `nullif(email, '')` lets every '' through). No
        injectivity is needed: `lower(email)` unique makes `email` unique."""
        bindings = tuple(_Binding(relation.name.lower(), column.name.lower(), column) for column in relation.output_columns)
        found: dict[ir.OutputColumn, None] = {}
        for part in parts:
            lowered = self._lower_expression(part, bindings, schema_complete=relation.is_schema_complete)
            inputs = ir.determinants(lowered)
            text = part.sql(dialect=self.dialect)
            if inputs is None:
                problem = f"{text} is not a function of the table's columns"
            elif not ir.total(lowered):
                problem = f"{text} may be NULL where its columns are not, which exempts the row from the index"
            elif not all(column in relation.output_columns and column.identity_comparison for column in inputs):
                problem = f"{text} reads a column whose equal values may differ (a float, or text under a collation)"
            else:
                found.update(dict.fromkeys(inputs))
                continue
            self._unsupported_create(index, f"a unique index whose key states no Unique Set ({problem})")
            return None
        return tuple(found)

    def _key_trust(self, constraint: Expr, symbol: _Symbol, columns: tuple[str, ...]) -> Literal["enforced", "declared"] | None:
        """Whether a key or foreign key constraint holds: enforced by the
        database (Knowledge), trusted by its author (a Property Declaration:
        marked `RELY`, or any unenforced one under `trust_unenforced_keys`),
        or neither, which a warning reports. An unenforced one is never
        Knowledge, so never proves anything by itself (ADR 0005)."""
        assert symbol.statement is not None
        foreign = isinstance(constraint, exp.ForeignKey | exp.Reference)
        check = isinstance(constraint, exp.CheckColumnConstraint)
        reference = constraint.args.get("reference") if isinstance(constraint, exp.ForeignKey) else constraint
        options = {str(option).upper() for option in (reference.args.get("options") or ())} if isinstance(reference, Expr) else set()
        meaning = self.semantics
        reason = None
        if "NOT ENFORCED" in options:
            reason = "it is declared NOT ENFORCED"
        elif id(constraint) in self._unvalidated:
            reason = "ALTER TABLE ... NOT VALID added it, so rows already in the table were never checked"
        elif "DEFERRABLE" in options or "INITIALLY DEFERRED" in options:
            reason = "it is DEFERRABLE, so a transaction may see it violated"
        elif (meaning.check_constraints if check else meaning.foreign_keys if foreign else meaning.unique_keys) != "enforced":
            reason = f"{self.dialect} does not enforce it"
        elif isinstance(constraint, exp.Reference) and not meaning.inline_references:
            reason = f"{self.dialect} before 9.0 ignores a column-level REFERENCES"
        elif foreign and meaning.foreign_key_engines is not None and (engine := _engine(symbol)) not in meaning.foreign_key_engines:
            reason = (
                f"ENGINE={engine} does not enforce foreign keys" if engine is not None
                else "its table names no ENGINE, and the server's default_storage_engine may not enforce foreign keys"
            )
        if reason is None:
            return "enforced"
        if ("RELY" in options and meaning.rely) or self.trust_unenforced_keys:
            return "declared"
        kind = "check constraint" if check else "foreign key" if foreign else "primary key" if isinstance(constraint, exp.PrimaryKey | exp.PrimaryKeyColumnConstraint) else "unique constraint"
        fixes = ["mark it RELY"] if meaning.rely and not check else []
        if foreign and meaning.foreign_key_engines is not None and _engine(symbol) is None:
            fixes.append("write ENGINE=InnoDB")
        fixes.append("analyze with trust_unenforced_keys=True")
        self._warnings.append(Diagnostic(
            diag.UNENFORCED_CONSTRAINT,
            f"the {kind} ({quoted_all(columns)}) of {symbol.name} is not Knowledge, since {reason}: "
            f"if the data satisfies it, {' or '.join(fixes)}",
            self._origin(constraint, _source_line(constraint) or _source_line(symbol.statement)),
        ))
        return None

    def _origin(self, node: Expr, line: int | None = None) -> Origin:
        return Origin(SQL, written_sql(node, self.dialect), line)


def _pass_output_columns(inputs: tuple[ir.OutputColumn, ...], origin: Origin) -> tuple[ir.OutputColumn, ...]:
    return tuple(
        ir.OutputColumn(origin=origin, name=column.name, scalar_expr=ir.ColumnRef(origin=origin, column=column))
        for column in inputs
    )


def _source_line(node: Expr) -> int | None:
    """The first source line of `node`, from the positions SQLGlot keeps on its tokens."""
    lines = [descendant.meta["line"] for descendant in node.walk() if "line" in descendant.meta]
    return min(lines, default=None)


def _subquery_conjunct(conjunct: Expr) -> tuple[ir.JoinKind, exp.Select, Expr | None] | None:
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
def dialect_builtins(meaning: DialectSemantics) -> dict[type[Expr], tuple[ir.Builtin, tuple[str, ...]]]:
    """Each modelled Builtin, by the SQLGlot node it lowers from (exactly that
    type: TRY_CAST, a subclass of CAST, may be NULL for a value), with the
    argument keys its parameters come from, a list key in place of a variadic
    Builtin's repeated parameter, as a dialect's semantics define it
    (`dialects.py`; `scripts/certify_dialects.py` probes each Builtin fact).

    Comparisons, LIKE, NOT, BETWEEN and the bitwise operators are never NULL
    for non-null arguments where a failed implicit cast raises (`casts_raise`;
    in Hive `1 < 'x'` is NULL). Arithmetic and CAST only
    where they raise an error rather than return NULL (`checked_arithmetic`:
    SQLite turns a NaN into NULL, MySQL a bad date CAST), and division and
    remainder only where division by zero raises too (`checked_division`).
    A CAST from JSON is not total anywhere (`convert._is_json`): JSON `null`
    is a value that casts to NULL. `+`, `-` and negation are injective where exact arithmetic overflows into
    an error. `||`, `concat()`, `concat_ws()`, `greatest()`, `least()` and
    subscripts (`xs[1]`) as each dialect treats NULL (MySQL's `||` is OR)."""
    checked = meaning.checked_arithmetic
    casts = meaning.casts_raise

    def builtin(name: str, arity: int, *, total: bool = True, injective: bool = False) -> ir.Builtin:
        return ir.Builtin(name=name, strict=(True,) * arity, total=(True,) * arity if total else None, injective=injective)

    binary = {
        exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=", exp.Like: "LIKE", exp.ILike: "ILIKE",
        exp.BitwiseAnd: "&", exp.BitwiseOr: "|", exp.BitwiseXor: "^", exp.BitwiseLeftShift: "<<", exp.BitwiseRightShift: ">>",
    }
    division = {exp.Div: "/", exp.IntDiv: "//", exp.Mod: "%"}
    builtins: dict[type[Expr], tuple[ir.Builtin, tuple[str, ...]]] = {
        **{node: (builtin(name, 2, total=casts), ("this", "expression")) for node, name in binary.items()},
        exp.Mul: (builtin("*", 2, total=checked), ("this", "expression")),
        **{node: (builtin(name, 2, total=meaning.checked_division), ("this", "expression")) for node, name in division.items()},
        **{node: (builtin(name, 2, total=checked, injective=checked), ("this", "expression")) for node, name in {exp.Add: "+", exp.Sub: "-"}.items()},
        exp.Neg: (builtin("unary -", 1, total=checked, injective=checked), ("this",)),
        exp.Not: (builtin("NOT", 1, total=casts), ("this",)),
        exp.BitwiseNot: (builtin("~", 1, total=casts), ("this",)),
        exp.Cast: (builtin("CAST", 1, total=checked), ("this",)),
        # `1 BETWEEN NULL AND 0` is FALSE.
        exp.Between: (ir.Builtin(name="BETWEEN", strict=(True, False, False), total=(True, True, True) if casts else None, injective=False), ("this", "low", "high")),
        # Text functions take any value as text, so no cast fails. `trim(' ')`
        # is '', which is NULL where '' is.
        exp.Lower: (builtin("lower", 1), ("this",)),
        exp.Upper: (builtin("upper", 1), ("this",)),
        exp.Length: (builtin("length", 1), ("this",)),
        exp.Trim: (builtin("trim", 1, total=not meaning.empty_string_is_null), ("this",)),
        # A number read from bad text is an error only where a bad CAST is
        # (SQLite's `floor('x')` is NULL though its comparisons raise).
        exp.Abs: (builtin("abs", 1, total=checked), ("this",)),
        exp.Floor: (builtin("floor", 1, total=checked), ("this",)),
        exp.Ceil: (builtin("ceil", 1, total=checked), ("this",)),
        # NULL when its arguments are equal; NULL only through the first otherwise.
        exp.Nullif: (ir.Builtin(name="nullif", strict=(True, False), total=None, injective=False), ("this", "expression")),
    }
    match meaning.pipes:
        case "strict":
            builtins[exp.DPipe] = (builtin("||", 2), ("this", "expression"))
        case "lenient":
            builtins[exp.DPipe] = (ir.Builtin(name="||", strict=(False, False), total=(True, True), injective=False), ("this", "expression"))
        case None:
            pass
        case _:
            assert_never(meaning.pipes)
    match meaning.concat:
        case "strict":
            builtins[exp.Concat] = (
                ir.BuiltinVariadic(name="concat", strict=(), total=(), injective=False, repeated_strict=True, repeated_total=True), ("expressions",),
            )
        case "skips-null":
            # `concat(NULL)` is ''.
            builtins[exp.Concat] = (
                ir.BuiltinVariadic(name="concat", strict=(), total=(), injective=False, repeated_strict=False, repeated_total=False), ("expressions",),
            )
        case None:
            pass
        case _:
            assert_never(meaning.concat)
    match meaning.greatest:
        case "skips-null":
            # NULL only when every argument is: `greatest(NULL, 1)` is 1.
            for node, name in {exp.Greatest: "greatest", exp.Least: "least"}.items():
                builtins[node] = (
                    ir.BuiltinVariadic(
                        name=name, strict=(False,), total=None, injective=False, repeated_strict=False, repeated_total=False, skips_null=True,
                    ),
                    ("this", "expressions"),
                )
        case "strict":
            # NULL when any argument is: `greatest(NULL, 1)` is NULL.
            for node, name in {exp.Greatest: "greatest", exp.Least: "least"}.items():
                builtins[node] = (
                    ir.BuiltinVariadic(name=name, strict=(True,), total=None, injective=False, repeated_strict=True, repeated_total=False),
                    ("this", "expressions"),
                )
        case None:
            pass
        case _:
            assert_never(meaning.greatest)
    if meaning.subscript_strict:
        # NULL for a NULL list, and for an index out of range or a NULL element.
        builtins[exp.Bracket] = (ir.Builtin(name="[]", strict=(True, False), total=None, injective=False), ("this", "expressions"))
    match meaning.concat_ws:
        case "strict":
            builtins[exp.ConcatWs] = (
                ir.BuiltinVariadic(name="concat_ws", strict=(True,), total=(True,), injective=False, repeated_strict=True, repeated_total=True),
                ("expressions",),
            )
        case "skips-null":
            # NULL values are skipped: `concat_ws(',', NULL)` is ''.
            builtins[exp.ConcatWs] = (
                ir.BuiltinVariadic(name="concat_ws", strict=(True,), total=(True,), injective=False, repeated_strict=False, repeated_total=False),
                ("expressions",),
            )
        case None:
            pass
        case _:
            assert_never(meaning.concat_ws)
    return builtins


def _as_list(value: Expr | list[Expr]) -> list[Expr]:
    return value if isinstance(value, list) else [value]


def _disjuncts(predicate: Expr) -> list[Expr]:
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


def _conjuncts(predicate: Expr) -> list[Expr]:
    """The operands of a predicate's top-level ANDs, parentheses removed."""
    while isinstance(predicate, exp.Paren):
        predicate = predicate.this
    if isinstance(predicate, exp.And):
        return _conjuncts(predicate.this) + _conjuncts(predicate.expression)
    return [predicate]


def _direct_sources(select: exp.Select) -> list[Expr]:
    """The sources of `select`'s FROM, those of a parenthesized join in place of it."""
    source = _from_source(select)
    return _flattened(([] if source is None else [source]) + [join.this for join in select.args.get("joins") or ()])


def _flattened(sources: list[Expr]) -> list[Expr]:
    flat: list[Expr] = []
    for source in sources:
        nested = _nested_join(source)
        flat.extend([source] if nested is None else _flattened([nested[0], *(join.this for join in nested[1])]))
    return flat


def _all_joins(select: exp.Select) -> list[exp.Join]:
    """The joins of `select`'s FROM, those inside a parenthesized join included."""
    return [join for joins, _ in _join_levels(select) for join in joins]


def _join_levels(select: exp.Select) -> list[tuple[list[exp.Join], list[Expr]]]:
    """The joins of `select`'s FROM and of each parenthesized join in it, each
    list with the sources it joins, flattened: a USING column is one of theirs."""
    source = _from_source(select)
    levels: list[tuple[list[exp.Join], list[Expr]]] = [(list(select.args.get("joins") or ()), _direct_sources(select))]
    pending: list[Expr | None] = [source, *(join.this for join in select.args.get("joins") or ())]
    while pending:
        source = pending.pop()
        nested = _nested_join(source) or _aliased_join(source)
        if nested is not None:
            first, joins = nested
            levels.append((joins, _flattened([first, *(join.this for join in joins)])))
            pending.extend([first, *(join.this for join in joins)])
    return levels


def _nested_join(source: Expr | None) -> tuple[Expr, list[exp.Join]] | None:
    """A parenthesized join, `(a JOIN b ON ...)` without an alias: its first
    source, which SQLGlot hangs the joins on, and those joins. Its columns keep
    the names and qualifiers of its sources. Not with an alias, which DuckDB
    reads as a subquery renaming repeated names (`x`, `x_1`) and other dialects
    as names that may be ambiguous."""
    if not isinstance(source, exp.Subquery) or any(value for key, value in source.args.items() if key not in {"this", "joins"}):
        return None
    first = source.this
    if not isinstance(first, exp.Table | exp.Subquery) or not first.args.get("joins"):
        return None
    return first, list(first.args["joins"])


def _aliased_join(source: Expr | None) -> tuple[Expr, list[exp.Join]] | None:
    """A parenthesized join with an alias, `(a JOIN b ON ...) AS x`, perhaps
    naming its columns: its first source and its joins, as `_nested_join`."""
    if not isinstance(source, exp.Subquery) or not source.alias or any(
        value for key, value in source.args.items() if key not in {"this", "alias"}
    ):
        return None
    first = source.this
    if not isinstance(first, exp.Table | exp.Subquery) or not first.args.get("joins"):
        return None
    return first, list(first.args["joins"])


def _hidden_sources(source: Expr) -> list[Expr]:
    """The sources inside an aliased parenthesized join, at any depth: their
    names are visible only inside it."""
    nested = _aliased_join(source)
    if nested is None:
        return []
    inner = _flattened([nested[0], *(join.this for join in nested[1])])
    return [*inner, *(hidden for source in inner for hidden in _hidden_sources(source))]


def _asserted_joins(ast: ParsedProgram) -> list[exp.Join]:
    statements = (*ast.create_statements, ast.root_select)
    return [
        join for statement in statements if statement is not None
        for join in statement.find_all(exp.Join) if assertion_span(join) is not None
    ]


def _asserted_selects(ast: ParsedProgram) -> list[exp.Query]:
    statements = (*ast.create_statements, ast.root_select)
    return [
        select for statement in statements if statement is not None
        for select in statement.find_all(exp.Select, exp.SetOperation) if column_markers(select)
    ]



def _asserted_cases(ast: ParsedProgram) -> list[exp.Case]:
    statements = (*ast.create_statements, ast.root_select)
    return [
        case for statement in statements if statement is not None
        for case in statement.find_all(exp.Case) if exhaustive_span(case) is not None
    ]


def _table_level_unique_columns(constraint: Expr) -> tuple[str, ...] | None:
    if isinstance(constraint, exp.PrimaryKey):
        return tuple(identifier.name for identifier in constraint.expressions)
    if isinstance(constraint, exp.UniqueColumnConstraint) and isinstance(constraint.this, exp.Schema):
        return tuple(identifier.name for identifier in constraint.this.expressions)
    return None


def _table_constraints(symbol: _Symbol) -> list[Expr]:
    """A declared table's table-level constraints, named or not: its CREATE
    TABLE's, then those added later."""
    assert symbol.statement is not None
    schema = symbol.statement.this
    own = _unwrap_constraints(schema.expressions) if isinstance(schema, exp.Schema) else []
    return own + symbol.constraints


def _unwrap_constraints(constraints: Iterable[Expr]) -> list[Expr]:
    """`CONSTRAINT name ...` is the constraints it names."""
    return [
        inner for constraint in constraints
        for inner in (constraint.expressions if isinstance(constraint, exp.Constraint) else (constraint,))
    ]


def _is_added_constraint(constraint: Expr) -> bool:
    return (
        isinstance(constraint, exp.PrimaryKey | exp.ForeignKey | exp.CheckColumnConstraint)
        or _table_level_unique_columns(constraint) is not None
    )


def _references(symbol: _Symbol) -> list[tuple[tuple[str, ...], exp.Table, tuple[str, ...] | None, Expr]]:
    """Every `REFERENCES` constraint of a table, column-level and table-level,
    as `(columns, referenced table, referenced columns, node)`. Referenced
    columns are None when omitted, meaning the referenced primary key."""
    found = []
    assert symbol.statement is not None
    for definition in symbol.statement.this.find_all(exp.ColumnDef):
        for constraint in definition.constraints:
            reference = constraint.args.get("kind")
            if isinstance(reference, exp.Reference):
                found.append(((definition.name,), *_reference_target(reference), reference))
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

_TEXT_TYPES = {exp.DataType.Type.VARCHAR, exp.DataType.Type.TEXT}


def _collated(definition: exp.ColumnDef) -> bool:
    """Whether a column is declared with a `COLLATE`."""
    return any(isinstance(constraint.args.get("kind"), exp.CollateColumnConstraint) for constraint in definition.constraints)


def _created_table(statement: exp.Create) -> exp.Table | None:
    target = statement.this
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None


def _is_named_table(source: Expr) -> TypeGuard[exp.Table]:
    """A FROM source naming a table, view or CTE. SQLGlot parses a
    table-valued function (`json_each(x)`, `read_parquet('f')`,
    `generate_series(1, 3)`) as a Table whose `this` is the call: its rows
    are opaque, like an UNNEST, and it names no relation."""
    return isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier)


def _column_aliases(source: Expr) -> tuple[str, ...]:
    alias = source.args.get("alias")
    return tuple(column.name for column in alias.columns) if isinstance(alias, exp.TableAlias) else ()


def _qualified_name(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)


def _unwrap_query(body: Expr | None) -> exp.Query | None:
    while isinstance(body, exp.Subquery):
        body = body.this
    return body if isinstance(body, exp.Query) else None


def _arg(node: Expr, name: str) -> Expr | None:
    return node.args.get(f"{name}_") or node.args.get(name)


def _from_source(select: exp.Query) -> Expr | None:
    source = _arg(select, "from")
    return source.this if isinstance(source, exp.From) else None


def _unsupported_grouping(select: exp.Select) -> str | None:
    """The grouping construct that makes `select` opaque, if any."""
    group = select.args.get("group")
    if group is None:
        return None
    for key, name in (("grouping_sets", "GROUPING SETS"), ("rollup", "ROLLUP"), ("cube", "CUBE"), ("totals", "WITH TOTALS")):
        if group.args.get(key):
            return name
    for node, name in ((exp.GroupingSets, "GROUPING SETS"), (exp.Rollup, "ROLLUP"), (exp.Cube, "CUBE")):
        if any(isinstance(expression, node) for expression in group.expressions):
            return name
    return None


def _unsupported_source(source: Expr) -> str | None:
    """The FROM source construct that lowers opaque, if sqlassert does not support it yet."""
    if isinstance(source, exp.Lateral) or source.find(exp.Lateral) is not None:
        return "LATERAL"
    if isinstance(source, exp.Unnest) or source.find(exp.Unnest) is not None:
        return "UNNEST"
    if isinstance(source, exp.Table) and isinstance(source.this, exp.Func):
        return "a table function"
    return None


def _traced_comparison_type(column: ir.OutputColumn) -> str | None:
    """The declared comparison type of the column `column` passes on."""
    while column.comparison_type is None and isinstance(column.scalar_expr, ir.ColumnRef):
        column = column.scalar_expr.column
    return column.comparison_type


# Rows of a VALUES list past which its columns get no Enumerated Domain.
_VALUES_DOMAIN_ROWS = 100

# A grouping key not selected, kept as a hidden Aggregate output: no SQL name can match it.
HIDDEN_GROUPING = "\0grouping"

# An aggregate HAVING reads that no item writes, a hidden Aggregate output too.
HIDDEN_AGGREGATE = "\0aggregate"

# A USING column's merged binding (`_merge_using`): no qualifier can name it.
_USING_QUALIFIER = "\0using"

# A scalar subquery's value binding: no SQL name can match it.
_SCALAR_QUALIFIER = "\0scalar"
# Aggregates whose value over no rows is NULL, so a decorrelated subquery's
# padding is that value. Not count_if, approx_count_distinct and the like: 0.
_EMPTY_NULL_AGGREGATES = (
    exp.Sum | exp.Avg | exp.Min | exp.Max | exp.AnyValue | exp.LogicalAnd | exp.LogicalOr
    | exp.ArgMax | exp.ArgMin | exp.First | exp.Last | exp.Stddev | exp.Variance
)


# A set-returning call's value binding: no SQL name can match it.
_SET_RETURNING_QUALIFIER = "\0rows"

# Calls that return rows, not a value, in a select list: `unnest(arr)`,
# Spark's `explode(arr)`, Postgres's `generate_series(1, 3)`. Not DuckDB's
# `generate_series`, which there returns a list.
_SET_RETURNING = (exp.Explode, exp.Inline, exp.Stack, exp.ExplodingGenerateSeries, exp.Unnest)

# Set-returning functions SQLGlot parses as anonymous calls, by dialect.
_SET_RETURNING_NAMES: dict[str, frozenset[str]] = {
    "postgres": frozenset({
        "generate_subscripts", "json_array_elements", "json_array_elements_text", "json_each", "json_each_text",
        "json_object_keys", "json_populate_recordset", "json_to_recordset", "jsonb_array_elements", "jsonb_array_elements_text",
        "jsonb_each", "jsonb_each_text", "jsonb_object_keys", "jsonb_path_query", "jsonb_populate_recordset", "jsonb_to_recordset",
        "regexp_matches", "regexp_split_to_table", "string_to_table",
    }),
    "spark": frozenset({"json_tuple", "inline_outer"}),
    "databricks": frozenset({"json_tuple", "inline_outer"}),
    "hive": frozenset({"json_tuple", "inline_outer"}),
}


def _set_returning_calls(item: Expr, dialect: str) -> list[Expr]:
    """The outermost set-returning calls a select list item evaluates itself:
    not one inside a subquery, which returns rows of its own."""
    names = _SET_RETURNING_NAMES.get(dialect, frozenset())
    calls: list[Expr] = []

    def returns_rows(node: Expr) -> bool:
        return isinstance(node, _SET_RETURNING) or (isinstance(node, exp.Anonymous) and node.name.lower() in names)

    for node in item.walk(prune=lambda node: isinstance(node, exp.Query | exp.Subquery) or returns_rows(node)):
        if returns_rows(node):
            calls.append(node)
    return calls


def _scalar_subqueries(expression: Expr) -> list[exp.Subquery]:
    """The scalar subqueries `expression` evaluates itself: not one under
    EXISTS, IN, ANY or ALL, nor inside another subquery."""
    found = []
    for node in expression.walk(prune=lambda node: isinstance(node, (exp.Subquery, exp.Query, exp.Exists))):
        if isinstance(node, exp.Subquery) and not isinstance(node.parent, (exp.In, exp.Exists, exp.Any, exp.All)):
            found.append(node)
    return found


def _in_scope(column: ir.OutputColumn, scope: ir.RelationExpr) -> ir.OutputColumn | None:
    """`column` as a column of `scope`: itself, or the input column it passes on."""
    if column in scope.output_columns:
        return column
    match column.scalar_expr:
        case ir.ColumnRef(column=upstream) if upstream in scope.output_columns:
            return upstream
        case _:
            return None


def _carrying(column: ir.OutputColumn, scope: ir.RelationExpr) -> ir.OutputColumn | None:
    """The one column of `scope` that passes `column` on, through any stages."""
    found = []
    for candidate in scope.output_columns:
        current = candidate
        while current is not column and isinstance(current.scalar_expr, ir.ColumnRef):
            current = current.scalar_expr.column
        if current is column:
            found.append(candidate)
    return found[0] if len(found) == 1 else None


def _schema_path(node: exp.Table | exp.Column) -> str:
    """The catalog and schema a table or column is written with, as `s1` or `c.s1`."""
    return ".".join(part for part in (node.catalog, node.db) if part).lower()


def _star_only(select: exp.Select) -> bool:
    return len(select.expressions) == 1 and isinstance(select.expressions[0], exp.Star) and not any(select.expressions[0].args.values())


def _selected_output_names(select: exp.Select) -> tuple[str, ...]:
    return tuple(item.alias_or_name for item in select.expressions if not isinstance(item, exp.Star))


def _matching_output(group_expression: Expr, items: list[Expr]) -> Expr | None:
    return next(
        (item for item in items if (item.this if isinstance(item, exp.Alias) else item) == group_expression),
        None,
    )


def _partition_positions(partitions: Sequence[Expr], items: list[Expr]) -> list[int] | None:
    """The position of the select item each partition key is written as, or
    None when one is not an item, or is a bare name another item is aliased
    to (it may read that alias instead)."""
    positions = []
    for partition in partitions:
        item = _matching_output(partition, items)
        if item is None:
            return None
        if isinstance(partition, exp.Column) and not partition.table and any(
            other is not item and other.alias_or_name.lower() == partition.name.lower() for other in items
        ):
            return None
        positions.append(next(index for index, other in enumerate(items) if other is item))
    return positions


def _same_column_alias(group_expression: Expr, items: list[Expr]) -> Expr | None:
    """The item `GROUP BY x` names when `x` is both the item's alias and the
    column it selects (`t.x AS x`): either reading of the name is that column."""
    if not isinstance(group_expression, exp.Column) or group_expression.table:
        return None
    name = group_expression.name.lower()
    return next((
        item for item in items
        if isinstance(item, exp.Alias) and item.alias.lower() == name and isinstance(item.this, exp.Column) and item.this.name.lower() == name
    ), None)


def _matching_grouping_output(group_expression: Expr, items: list[Expr]) -> Expr | None:
    if isinstance(group_expression, exp.Literal) and group_expression.is_int:
        position = int(group_expression.this)
        return items[position - 1] if 1 <= position <= len(items) else None
    return _matching_output(group_expression, items)


def _aggregate_argument(expression: Expr, kinds: tuple[type[exp.AggFunc], ...], *, distinct: bool = False) -> Expr | None:
    """The one argument of a plain call of one of `kinds`: no FILTER, window,
    or other argument (DuckDB's `max(x, 3)` is a list), and no DISTINCT
    unless `distinct` allows one argument's."""
    if type(expression) not in kinds or isinstance(expression.this, exp.Star) or expression.this is None:
        return None
    if any(value for key, value in expression.args.items() if key != "this"):
        return None
    if isinstance(expression.this, exp.Distinct):
        arguments = expression.this.expressions
        return arguments[0] if distinct and len(arguments) == 1 and not expression.this.args.get("on") else None
    return expression.this


def _picked_argument(expression: Expr) -> Expr | None:
    """The argument of an aggregate whose value is the argument's value on
    some row of the group: `min` and `max` (a non-null one when there is one;
    of distinct values or not, the same) and `any_value`, whatever its NULL
    treatment (DuckDB parses its own as IGNORE NULLS). Not BigQuery's
    `any_value(x HAVING MAX y)`, opaque."""
    if isinstance(expression, (exp.IgnoreNulls, exp.RespectNulls)):
        expression = expression.this
    argument = _aggregate_argument(expression, (exp.Min, exp.Max), distinct=True) or _aggregate_argument(expression, (exp.AnyValue,))
    return None if isinstance(argument, exp.HavingMax) else argument


def _outermost_aggregate_calls(expression: Expr) -> list[Expr]:
    """The aggregate calls of `expression` not inside another, a window or a
    subquery, with the FILTER or NULL treatment written on them."""
    calls: list[Expr] = []
    pending: list[Expr] = [expression]
    while pending:
        node = pending.pop()
        if _is_aggregate_call(node):
            calls.append(node)
        elif not isinstance(node, exp.Window | exp.Query | exp.Subquery):
            pending.extend(node.iter_expressions())
    return calls


def _unaliased(item: Expr) -> Expr:
    return item.this if isinstance(item, exp.Alias) else item


def _is_aggregate_call(node: Expr) -> bool:
    """An aggregate call, with the FILTER or NULL treatment written on it."""
    while isinstance(node, exp.Filter | exp.IgnoreNulls | exp.RespectNulls | exp.WithinGroup):
        node = node.this
    return isinstance(node, exp.AggFunc)


def _reads_ungrouped_column(lowered: ir.ScalarExpr, determined: Callable[[ir.OutputColumn], bool]) -> bool:
    """Whether `lowered`, an item of a grouped select, reads a column outside
    every aggregate that the grouping columns do not determine (`determined`).
    Windows and subqueries lower opaque, and an aggregate's argument is read
    per row, so every other column `lowered` reads is outside them."""
    pending: list[ir.ScalarExpr] = [lowered]
    while pending:
        node = pending.pop()
        if isinstance(node, ir.ColumnRef):
            if not determined(node.column):
                return True
            continue
        if isinstance(node, ir.AnyAggregate | ir.Accumulation | ir.Count):
            continue
        pending.extend(child for child in ir.children(node) if isinstance(child, ir.ScalarExpr))
    return False


def _row_number_equals_one(predicate: Expr) -> exp.Window | None:
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


def _last_select(query: Expr) -> exp.Select | None:
    """The Select a set operation's left input ends with."""
    while True:
        unwrapped = _unwrap_query(query) if isinstance(query, exp.Query) else None
        match unwrapped:
            case exp.SetOperation():
                query = unwrapped.expression
            case exp.Select():
                return unwrapped
            case _:
                return None


_SET_OPERATORS: dict[type[Expr], ir.SetOperator] = {exp.Union: "union", exp.Intersect: "intersect", exp.Except: "except"}


# Types whose equal values are identical: no collation (text has a comparison
# type only when compared by identity), no signed zero or NaN, no two
# spellings of one interval.
_IDENTITY_TYPES = {
    *exp.DataType.INTEGER_TYPES, *exp.DataType.TEXT_TYPES,
    exp.DataType.Type.DECIMAL, exp.DataType.Type.BOOLEAN, exp.DataType.Type.DATE, exp.DataType.Type.TIME,
    exp.DataType.Type.TIMESTAMP, exp.DataType.Type.UUID,
}
# TIMESTAMPTZ is too, where it holds an instant only (`zoned_timestamp_is_instant`).

# Types whose values keep the zone they were given, where a TIMESTAMP WITH
# TIME ZONE is not an instant only: a CAST to another type reads that zone.
_ZONED_TYPES = {exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.TIMETZ}

# Types whose `null` (JSON's, a VARIANT's) is a non-null value that a CAST turns into NULL.
_JSON_TYPES = {exp.DataType.Type.JSON, exp.DataType.Type.JSONB, exp.DataType.Type.VARIANT, exp.DataType.Type.OBJECT}

# Composite types: a comparison of their values may be NULL for a NULL element.
_COMPOSITE_TYPES = {exp.DataType.Type.ARRAY, exp.DataType.Type.LIST, exp.DataType.Type.STRUCT, exp.DataType.Type.MAP, exp.DataType.Type.NESTED}

# The Builtins that compare two values, which compare composites element by element.
# Never NULL, in every dialect: a NULL operand is a value they compare.
_IS_DISTINCT_FROM = ir.Builtin(name="IS DISTINCT FROM", strict=(False, False), total=(False, False), injective=False)
# Never NULL: TRUE or FALSE, whatever its subquery returns.
_EXISTS = ir.Builtin(name="EXISTS", strict=(False,), total=(False,), injective=False)
_ORDER_COMPARISONS = {"<>", ">", ">=", "<", "<=", "BETWEEN"}
# Never NULL: `NULL IS TRUE` is FALSE, so never TRUE where the operand is NULL.
_IS_TRUE = ir.Builtin(name="IS TRUE", strict=(False,), total=(False,), injective=False, true_only_if_argument=True)
_IS_FALSE = ir.Builtin(name="IS FALSE", strict=(False,), total=(False,), injective=False, rejecting=(True,))


@cache
def _any_comparison(node: type[Expr]) -> ir.Builtin:
    """`x op ANY (...)`, in every dialect: FALSE over no values, even for a
    NULL `x`, else NULL where `x` is, so never TRUE where `x` is NULL. Not
    `ALL`, which is TRUE over no values."""
    names: dict[type[Expr], str] = {exp.EQ: "=", exp.NEQ: "<>", exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<="}
    name = names[node]
    return ir.Builtin(name=f"{name} ANY", strict=(False, False), total=None, injective=False, rejecting=(True, False))

# Comparisons, whose operands a zoned timestamp's text may be.
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ, exp.In, exp.Like, exp.ILike, exp.Between)

_EXACT_NUMERIC_TYPES = {*exp.DataType.INTEGER_TYPES, exp.DataType.Type.DECIMAL}

# The largest value of each signed integer type, the same in every dialect.
_INTEGER_MAXIMA = {
    exp.DataType.Type.TINYINT: 2**7 - 1, exp.DataType.Type.SMALLINT: 2**15 - 1,
    exp.DataType.Type.INT: 2**31 - 1, exp.DataType.Type.BIGINT: 2**63 - 1,
}


def _cast_integer_literal(expression: Expr) -> ir.LiteralValue | None:
    """The value of `CAST('1' AS INTEGER)` or `CAST(1 AS BIGINT)`: a
    non-negative integer, written as a number or a string with no sign,
    leading zero or space, within a signed integer type's range. That is the
    same integer in every dialect; a decimal (`CAST('1.5' AS INTEGER)` rounds
    or truncates by dialect) or any other cast is not folded."""
    if type(expression) is not exp.Cast:  # TRY_CAST and the like are not modelled
        return None
    to = expression.args.get("to")
    maximum = _INTEGER_MAXIMA.get(to.this) if isinstance(to, exp.DataType) else None
    value = literal_value(expression.this)
    if maximum is None or value is None or value.type == "boolean" or not _PLAIN_INTEGER.fullmatch(value.text):
        return None
    return ir.LiteralValue(type="numeric", text=value.text) if int(value.text) <= maximum else None


_PLAIN_INTEGER = re.compile(r"0|[1-9][0-9]{0,18}")

# Nodes whose value is not a function of the columns an expression reads.
# Builtins modelled only when the call sets no argument but their own.
_ONE_ARGUMENT_BUILTINS = (exp.Lower, exp.Upper, exp.Length, exp.Trim, exp.Abs, exp.Floor, exp.Ceil)

_NOT_A_FUNCTION_OF_ITS_COLUMNS = (
    exp.Anonymous, exp.Rand, exp.Randn, exp.Uuid, exp.AggFunc, exp.Filter, exp.WithinGroup, exp.Window, exp.Query, exp.Subquery,
    exp.CurrentDate, exp.CurrentDatetime, exp.CurrentTime, exp.CurrentTimestamp, exp.Placeholder, exp.Parameter, exp.Star,
)


def _enum_type_values(definition: exp.ColumnDef, meaning: DialectSemantics) -> frozenset[ir.LiteralValue] | None:
    """The values of a column declared `ENUM('a', ...)`: its members, with
    their trailing spaces removed where the dialect does (MySQL)."""
    kind = definition.args.get("kind")
    if not (isinstance(kind, exp.DataType) and kind.this == exp.DataType.Type.ENUM and kind.expressions):
        return None
    values = [literal_value(member) for member in kind.expressions]
    if any(value is None or value.type != "string" for value in values):
        return None
    members = {value for value in values if value is not None}
    if meaning.enum_strips_trailing_spaces:
        members = {replace(value, text=value.text.rstrip(" ")) for value in members}
    return frozenset(members)


def _check_domains(condition: Expr, meaning: DialectSemantics) -> list[tuple[str, frozenset[ir.LiteralValue]]] | None:
    """The Enumerated Domains a CHECK condition states, by lower-cased
    column name, or None for a shape that states none sqlassert reads.

    A CHECK passes a row unless its condition is FALSE, so NULL passes. Each
    AND-ed `col IN (v, ...)` or `col = v` over non-null literals is FALSE for
    a non-null value equal to no v, which makes the whole condition FALSE: so
    every non-null value of col equals one of them, exactly what ENUM states.
    A NULL among the values would make the IN NULL instead, and pass any row.
    """
    parts = list(condition.flatten()) if isinstance(condition, exp.And) else [condition]
    found = []
    for part in parts:
        match part.unnest():
            case exp.EQ(this=exp.Column() as column, expression=value) | exp.EQ(this=value, expression=exp.Column() as column):
                values = [value]
            case exp.In(this=exp.Column() as column, expressions=list(values)) if values and part.unnest().args.get("query") is None:
                pass
            case _:
                return None
        literals = [literal_value(value) for value in values]
        if column.table or any(
            literal is None or (meaning.empty_string_is_null and literal.type == "string" and literal.text == "")
            for literal in literals
        ):
            return None
        found.append((column.name.lower(), frozenset(literal for literal in literals if literal is not None)))
    return found


def _engine(symbol: _Symbol) -> str | None:
    """The storage ENGINE a CREATE TABLE names (lower-cased), if any."""
    assert symbol.statement is not None
    engine = symbol.statement.find(exp.EngineProperty)
    return engine.name.lower() if engine is not None else None


def _aggregate_arguments(call: exp.AggFunc) -> list[Expr]:
    """The value arguments of an aggregate call: none for `count(*)`, the
    listed ones under DISTINCT."""
    this = call.this
    if this is None or isinstance(this, exp.Star):
        return []
    if isinstance(this, exp.Distinct):
        return list(this.expressions)
    return [this, *call.expressions]


def _is_positional(join: exp.Join) -> bool:
    return (join.args.get("method") or "").lower() == "positional"


def _join_kind(join: exp.Join, dialect: str) -> ir.JoinKind | None:
    """SEMI, ANTI and CROSS whatever the side; otherwise the side, else INNER
    (`STRAIGHT_JOIN`, and ASOF, which keeps a subset of the inner join's rows).
    None for a join whose semantics are not modelled: it lowers opaque."""
    if _is_positional(join):
        return "full"
    kind, side = (join.args.get("kind") or "").lower(), (join.args.get("side") or "").lower()
    if dialect == "clickhouse" and (side or kind not in {"", "inner", "cross"}):
        # TODO: check ClickHouse semantics. Outer joins pad with the column
        # type's default, not NULL, unless join_use_nulls is set; SEMI and ANTI
        # joins keep the right columns; ANY and ASOF pick one match.
        return None
    match kind, side:
        case ("semi" | "anti" | "cross") as kind, _:
            return kind
        case "" | "inner" | "outer" | "straight_join", ("left" | "right" | "full") as side:
            return side
        case "outer", "":
            return "full"
        case "" | "inner" | "straight_join", "":
            return "inner"
        case _:
            # TODO: check the semantics of each dialect's other join kinds
            # (ClickHouse ARRAY JOIN, ANY, ...) before modelling one.
            return None


def _join_label(join: exp.Join, dialect: str) -> str:
    keywords = " ".join(part for part in (join.side, join.kind, "JOIN") if part)
    source = join.this
    if _nested_join(source) is not None:
        return f"{keywords} ({', '.join(part.alias_or_name for part in _flattened([source]))})"
    if isinstance(source, exp.Subquery):
        return f"{keywords} subquery {source.alias}".rstrip()
    return f"{keywords} {source.sql(dialect=dialect)}"


def _select_label(select: exp.Query) -> str:
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
