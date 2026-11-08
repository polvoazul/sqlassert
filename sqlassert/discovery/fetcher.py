"""Hydrate a SQL Program's external Relations from a database catalog.

A `DatabaseKnowledgeFetcher` runs before the engine. It links the Program once
without a connection to find the external Named Relations it reaches, asks
its database-specific hooks for their catalog metadata and view definitions,
and relinks the Program with that metadata turned into ordinary declarations:
a fetched table becomes a column-only `CREATE TABLE`, a fetched view the
`CREATE VIEW` the database stored. Each relink reveals the Relations the new
view bodies read, so view expansion recurses until nothing new is reached; a
cycle among views is left to the linker, which reports it.

Catalog constraints never pass through those declarations. `bind` turns them
into Properties over the final IR's own Output Columns, so the engine sees
Knowledge only, never a name, a catalog row, or a connection.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from sqlassert import diagnostics as diag
from sqlassert import ir
from sqlassert.diagnostics import Diagnostic
from sqlassert.ir.convert import IrConversionResult, IrParser
from sqlassert.properties import NonNullColumn, Property, ReferentialCoverage, UniqueSet
from sqlassert.provenance import Origin
from sqlassert.sql_parse import ParsedProgram, SqlParser

CATALOG = "catalog"


class DatabaseRelationKind(Enum):
    TABLE = "table"
    VIEW = "view"


@dataclass(frozen=True)
class DatabaseColumn:
    """One catalog column. `data_type` is its SQL type as the database writes
    it (in the fetcher's `type_dialect`), or None when unknown; `collation` is
    the collation its comparisons use, None for the database's binary default.
    A fetcher that cannot tell a column's collation leaves `data_type` None,
    so no comparison is assumed to be by identity."""

    name: str
    nullable: bool
    data_type: str | None = None
    collation: str | None = None


@dataclass(frozen=True)
class DatabaseForeignKey:
    """An enforced foreign key: `columns` pair by position with `referenced_columns`
    of the Relation whose canonical identity is `referenced`."""

    columns: tuple[str, ...]
    referenced: str
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True)
class DatabaseRelationMetadata:
    """One catalog Relation, identified canonically, with its complete enforced facts.

    Columns, keys, unique sets, and foreign keys describe base tables only; a
    view's are derived from its definition instead.
    """

    catalog: str
    schema: str
    name: str
    kind: DatabaseRelationKind
    columns: tuple[DatabaseColumn, ...] = ()
    primary_key: tuple[str, ...] = ()
    unique_sets: tuple[tuple[str, ...], ...] = ()
    foreign_keys: tuple[DatabaseForeignKey, ...] = ()

    @property
    def identity(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.name}"


@dataclass(frozen=True)
class DatabaseViewDefinition:
    """The `CREATE VIEW` statement a database stored for one view: structural input, never Knowledge."""

    sql: str


class DatabaseMetadataError(Exception):
    """The catalog could not be read, for example for lack of permission."""


@dataclass(frozen=True)
class DatabaseHydration:
    """The final linked Program and the catalog tables declared into it, by declared name."""

    conversion: IrConversionResult
    tables: Mapping[str, DatabaseRelationMetadata] = field(default_factory=dict)
    diagnostics: tuple[Diagnostic, ...] = ()


class DatabaseKnowledgeFetcher(ABC):
    """Generic catalog discovery; subclasses own one database's catalog boundary."""

    # The SQL dialect the database writes column types in.
    type_dialect: str | None = None

    def hydrate(self, ast: ParsedProgram, *, dialect: str, trust_unenforced_keys: bool = False) -> DatabaseHydration:
        return _Hydration(self, ast, dialect, trust_unenforced_keys).run()

    def bind(self, program: ir.Program, hydration: DatabaseHydration) -> tuple[Property, ...]:
        """Catalog facts as Properties over ``program``'s Output Columns.

        A foreign key binds only when the Program reaches its referenced Relation.
        """
        facts: list[Property] = []
        tables = {
            metadata.identity.lower(): (relation, metadata)
            for relation in program.named_relations
            if relation.role is ir.RelationRole.TABLE and (metadata := hydration.tables.get(relation.name.lower())) is not None
        }
        for relation, metadata in tables.values():
            columns = {column.name.lower(): column for column in relation.output_columns}
            non_null = {column.name.lower() for column in metadata.columns if not column.nullable}
            non_null.update(name.lower() for name in metadata.primary_key)
            facts.extend(NonNullColumn(column=columns[name]) for name in sorted(non_null) if name in columns)
            for unique_set in _unique_sets(metadata):
                facts.append(UniqueSet(columns=frozenset(columns[name.lower()] for name in unique_set)))
            for foreign_key in metadata.foreign_keys:
                if foreign_key.referenced.lower() not in tables:
                    continue
                target, target_metadata = tables[foreign_key.referenced.lower()]
                if _incomplete_reference(foreign_key, target_metadata):
                    continue  # Never seen from a consistent catalog; no fact is safer than a wrong one.
                referenced = {column.name.lower(): column for column in target.output_columns}
                facts.append(ReferentialCoverage(
                    columns=tuple(columns[name.lower()] for name in foreign_key.columns),
                    referenced_columns=tuple(referenced[name.lower()] for name in foreign_key.referenced_columns),
                ))
        return tuple(facts)

    @abstractmethod
    def fetch_relation_metadata(
        self, names: tuple[str, ...], scope: DatabaseRelationMetadata | None = None
    ) -> Mapping[str, DatabaseRelationMetadata]:
        """Resolve query-visible ``names`` and fetch each one's complete metadata.

        ``scope`` is the view whose definition contains the names, or None for
        the SQL Program itself. A name absent from the result does not exist.
        Raises DatabaseMetadataError when the catalog cannot be read.
        """

    @abstractmethod
    def fetch_view_definition(self, relation: DatabaseRelationMetadata) -> DatabaseViewDefinition:
        """The stored definition of one view. Raises DatabaseMetadataError when unreadable."""


def _unique_sets(metadata: DatabaseRelationMetadata) -> tuple[tuple[str, ...], ...]:
    return ((metadata.primary_key,) if metadata.primary_key else ()) + metadata.unique_sets


def _incompleteness(metadata: DatabaseRelationMetadata) -> str | None:
    if metadata.kind is not DatabaseRelationKind.TABLE:
        return None
    if not metadata.columns:
        return "has no columns"
    names = {column.name.lower() for column in metadata.columns}
    for unique_set in _unique_sets(metadata):
        if not unique_set or not {name.lower() for name in unique_set} <= names:
            return f"has a key ({', '.join(unique_set)}) over columns it does not list"
    for foreign_key in metadata.foreign_keys:
        if not {name.lower() for name in foreign_key.columns} <= names or len(foreign_key.columns) != len(foreign_key.referenced_columns):
            return f"has a foreign key ({', '.join(foreign_key.columns)}) that does not pair columns it lists"
    return None


def _incomplete_reference(foreign_key: DatabaseForeignKey, target: DatabaseRelationMetadata) -> bool:
    names = {column.name.lower() for column in target.columns}
    return not {name.lower() for name in foreign_key.referenced_columns} <= names


class _Hydration:
    """The state of hydrating one SQL Program: catalog answers are memoized for it alone."""

    def __init__(self, fetcher: DatabaseKnowledgeFetcher, ast: ParsedProgram, dialect: str, trust_unenforced_keys: bool):
        self.fetcher = fetcher
        self.ast = ast
        self.dialect = dialect
        self.trust_unenforced_keys = trust_unenforced_keys
        self.sql_declared = {_qualified_name(table).lower() for table in map(_created_table, ast.create_statements) if table is not None}
        self.resolved: dict[str, DatabaseRelationMetadata | None] = {}
        self.views: dict[str, exp.Create | None] = {}
        self.declarations: dict[str, exp.Create] = {}
        self.tables: dict[str, DatabaseRelationMetadata] = {}
        self.unavailable: set[str] = set()
        self.diagnostics: list[Diagnostic] = []

    def run(self) -> DatabaseHydration:
        while True:
            program = ParsedProgram((*self.declarations.values(), *self.ast.create_statements), self.ast.root_select)
            conversion = IrParser(self.dialect, self.trust_unenforced_keys).parse(program)
            pending = tuple(dict.fromkeys(
                relation.name for relation in conversion.program.named_relations if self._is_external(relation)
            ))
            if not pending:
                return DatabaseHydration(conversion, dict(self.tables), tuple(dict.fromkeys(self.diagnostics)))
            self.resolved.update(self._resolve(tuple(name for name in pending if name.lower() not in self.resolved), None))
            for name in pending:
                self._declare(name, self.resolved[name.lower()])

    def _is_external(self, relation: ir.NamedRelation) -> bool:
        key = relation.name.lower()
        return (
            relation.role is ir.RelationRole.TABLE
            and key not in self.sql_declared
            and key not in self.declarations
            and key not in self.unavailable
        )

    def _resolve(self, names: tuple[str, ...], scope: DatabaseRelationMetadata | None) -> dict[str, DatabaseRelationMetadata | None]:
        if not names:
            return {}
        where = f" read by view {scope.identity}" if scope is not None else ""
        try:
            found = self.fetcher.fetch_relation_metadata(names, scope)
        except DatabaseMetadataError as error:
            self.diagnostics.append(Diagnostic(diag.CATALOG_METADATA_UNAVAILABLE, f"catalog metadata for {', '.join(names)}{where} could not be read: {error}"))
            return dict.fromkeys((name.lower() for name in names), None)
        resolved: dict[str, DatabaseRelationMetadata | None] = {}
        for name in names:
            metadata = found.get(name)
            if metadata is None:
                self.diagnostics.append(Diagnostic(diag.CATALOG_RELATION_NOT_FOUND, f"relation {name}{where} is not in the database catalog"))
            resolved[name.lower()] = metadata
        return resolved

    def _declare(self, name: str, metadata: DatabaseRelationMetadata | None) -> None:
        key = name.lower()
        declaration = self._declaration(name, metadata) if metadata is not None else None
        if declaration is None:
            self.unavailable.add(key)
            return
        self.declarations[key] = declaration
        if metadata is not None and metadata.kind is DatabaseRelationKind.TABLE:
            self.tables[key] = metadata

    def _declaration(self, name: str, metadata: DatabaseRelationMetadata) -> exp.Create | None:
        problem = _incompleteness(metadata)
        if problem is not None:
            self.diagnostics.append(Diagnostic(diag.INCOMPLETE_CATALOG_METADATA, f"catalog relation {metadata.identity} {problem}", _origin(metadata)))
            return None
        target = _table(name)
        if metadata.kind is DatabaseRelationKind.TABLE:
            columns = [self._column_definition(column) for column in metadata.columns]
            return exp.Create(kind="TABLE", this=exp.Schema(this=target, expressions=columns))
        view = self._view(metadata)
        if view is None:
            return None
        declaration = view.copy()
        if isinstance(declaration.this, exp.Schema):
            declaration.this.set("this", target)
        else:
            declaration.set("this", target)
        return declaration

    def _column_definition(self, column: DatabaseColumn) -> exp.ColumnDef:
        """The column with its catalog type and collation, so the Program
        compares and enumerates it as the database does; untyped if the type
        does not parse."""
        definition = exp.ColumnDef(this=exp.to_identifier(column.name))
        if column.data_type is None:
            return definition
        try:
            kind = exp.DataType.build(column.data_type, dialect=self.fetcher.type_dialect)
        except SqlglotError:
            return definition
        definition.set("kind", kind)
        if column.collation is not None:
            collate = exp.CollateColumnConstraint(this=exp.to_identifier(column.collation))
            definition.append("constraints", exp.ColumnConstraint(kind=collate))
        return definition

    def _view(self, metadata: DatabaseRelationMetadata) -> exp.Create | None:
        """The view's definition with every Relation it reads named by canonical identity, memoized."""
        if metadata.identity not in self.views:
            self.views[metadata.identity] = self._fetch_view(metadata)
        return self.views[metadata.identity]

    def _fetch_view(self, metadata: DatabaseRelationMetadata) -> exp.Create | None:
        try:
            definition = self.fetcher.fetch_view_definition(metadata)
        except DatabaseMetadataError as error:
            self.diagnostics.append(Diagnostic(diag.CATALOG_METADATA_UNAVAILABLE, f"definition of view {metadata.identity} could not be read: {error}", _origin(metadata)))
            return None
        parsed = SqlParser(self.dialect).parse(definition.sql)
        self.diagnostics.extend(parsed.diagnostics)
        create = parsed.create_statements[0] if len(parsed.create_statements) == 1 and parsed.root_select is None else None
        body = create.args.get("expression") if isinstance(create, exp.Create) and (create.kind or "").upper() == "VIEW" else None
        if not isinstance(create, exp.Create) or not isinstance(body, exp.Query) or parsed.diagnostics:
            self.diagnostics.append(Diagnostic(diag.INCOMPLETE_CATALOG_METADATA, f"definition of view {metadata.identity} is not one analyzable CREATE VIEW statement", _origin(metadata)))
            return None
        try:
            tables = [source for scope in traverse_scope(body) for source in scope.sources.values() if isinstance(source, exp.Table)]
        except SqlglotError as error:
            self.diagnostics.append(Diagnostic(diag.INCOMPLETE_CATALOG_METADATA, f"definition of view {metadata.identity} could not be scoped: {error}", _origin(metadata)))
            return None

        # Names inside a view resolve in the view's own scope, so they are
        # rewritten to identities before they join the Program's global names.
        resolved = self._resolve(tuple(dict.fromkeys(_qualified_name(table) for table in tables)), metadata)
        if None in resolved.values():
            return None
        # A column qualified by its table's schema (MySQL stores `db`.`t`.`c`)
        # is qualified by the alias the table is given below.
        columns = [column for column in body.find_all(exp.Column) if column.args.get("db")]
        for table in tables:
            target = resolved[_qualified_name(table).lower()]
            assert target is not None
            for column in columns:
                if not table.alias and _qualified_name(table).lower().endswith(".".join((column.db, column.table)).lower()):
                    column.set("db", None)
                    column.set("catalog", None)
            self.resolved[target.identity.lower()] = target
            alias = table.alias or table.name
            table.set("this", exp.to_identifier(target.name))
            table.set("db", exp.to_identifier(target.schema))
            table.set("catalog", exp.to_identifier(target.catalog))
            table.set("alias", exp.TableAlias(this=exp.to_identifier(alias)))
        return create


def _origin(metadata: DatabaseRelationMetadata) -> Origin:
    return Origin(CATALOG, metadata.identity)


def _table(name: str) -> exp.Table:
    *qualifiers, table = name.split(".")
    catalog, db = ([None, None] + qualifiers)[-2:]
    return exp.table_(table, db=db, catalog=catalog)


def _created_table(statement: exp.Expression) -> exp.Table | None:
    target = statement.this if isinstance(statement, exp.Create) else None
    if isinstance(target, exp.Schema):
        target = target.this
    return target if isinstance(target, exp.Table) else None


def _qualified_name(table: exp.Table) -> str:
    return ".".join(part for part in (table.catalog, table.db, table.name) if part)
