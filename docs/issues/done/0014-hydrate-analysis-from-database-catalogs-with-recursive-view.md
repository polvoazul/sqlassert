# 14. Hydrate analysis from database catalogs with recursive view expansion

- **State:** done
- **Labels:** ready-for-agent
- **Created:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/14

## Problem Statement

Knowledge is a `tuple[Property, ...]` ([`properties.py`](../../../sqlassert/properties.py)) bound to IR nodes: `NonNullColumn`, `UniqueSet`, and friends hold direct `OutputColumn` and `Join` references. `IrParser` already produces it for SQL-declared schema, and `Engine.run(program, knowledge, diagnostics)` accepts it as `Accepted` evidence. It never contains a database name, catalog row, connection, cursor, or raw view SQL.

A caller with a live database still needs catalog facts for external Relations and view definitions. The adapter must resolve qualified database names and bind its metadata to the final Program IR before the property engine runs. The engine, the analysis functions in `sqlassert/rules/`, and the reporting path must not perform name resolution.

The initial implementation must fetch a complete snapshot of the currently supported fact families for the SQL Program's reachable external Relation closure. Fetching one fact at a time after failed proofs adds partial-knowledge states and repeated round trips without evidence that the complexity is useful.

## Solution

Introduce an inheritable `DatabaseKnowledgeFetcher` orchestration component and a DuckDB implementation.

```text
SQL Program
  -> parse
  -> preliminary IR conversion
  -> DatabaseKnowledgeFetcher consumes preliminary IR
       -> resolve reachable external Relations in the database
       -> fetch complete supported metadata
       -> fetch and parse view definitions recursively
  -> rebuild final immutable IR with fetched view declarations
  -> DatabaseKnowledgeFetcher binds metadata to final IR nodes
       -> tuple[Property, ...]
  -> Engine.run once
  -> Report
```

The preliminary IR is a discovery artifact. It identifies external `NamedRelation`s and required columns without a connection. The final IR is rebuilt rather than mutated, so fetched view bodies, bindings, output identities, and SQL-derived Knowledge are all created through the normal linker.

The fetcher owns the name-binding step. Given the final Program, it creates the normal linked public facts:

```python
NonNullColumn(column=users_id)
UniqueSet(columns=frozenset({users_id}))
```

The engine receives only `tuple[Property, ...]`. It does not know about qualified names, a catalog, or a connection.

## Generic Fetcher Contract

`DatabaseKnowledgeFetcher` owns generic discovery orchestration: it walks the preliminary IR, batches unresolved Relations, memoizes canonical identities, expands views recursively, detects cycles, parses fetched view SQL with the root dialect, and rebuilds the final Program.

Database-specific subclasses own the catalog boundary:

```python
class DatabaseKnowledgeFetcher(ABC):
    def hydrate(self, program: ir.Program) -> DatabaseHydration: ...
    def bind(self, program: ir.Program, hydration: DatabaseHydration) -> tuple[Property, ...]: ...

    @abstractmethod
    def fetch_relation_metadata(self, names: tuple[str, ...]) -> tuple[DatabaseRelationMetadata, ...]: ...

    @abstractmethod
    def fetch_view_definition(self, relation: DatabaseRelationMetadata) -> DatabaseViewDefinition: ...
```

Exact Python names may change, but these boundaries are required:

- Generic orchestration owns reachable-relation traversal, recursive view discovery, and final IR rebuild.
- The database adapter owns query-visible qualified-name resolution, catalog queries, relation-kind classification, and producing complete metadata.
- The binder returns only IR-bound `Property` objects.
- A fetched view definition is structural input, parsed and lowered through the existing SQL parser and IR converter; it is not Knowledge.

`DatabaseHydration` may retain database metadata and view definitions during hydration, but it is not part of the engine contract. The final bound Knowledge has no raw metadata objects or names.

## Decisions

- Add an opt-in connection-backed orchestration seam. Do not add a connection parameter to the engine, the IR model, `Knowledge`, or the pure `analyze(sql, *, dialect=...)` boundary.
- Do not reintroduce `RelationKnowledge`, `ColumnKnowledge`, `UniqueSetKnowledge`, or an `analyze(..., knowledge=...)` name-based API.
- SQL declarations take precedence over catalog objects with the same query-visible name and are never overwritten.
- Fetch complete columns, nullability, enforced Primary Keys, and enforced Unique Sets for every reachable external base table; do not fetch the whole catalog.
- Catalog name resolution and binding happen in the gatherer, outside the engine.
- The adapter reads metadata only. It never scans rows, samples data, or executes assertion queries.
- Run the engine once, after final IR conversion and Knowledge binding.

## Acceptance Criteria

- [ ] Generate preliminary IR before catalog access.
- [ ] `DatabaseKnowledgeFetcher` provides generic recursion/orchestration plus database-specific metadata and view hooks.
- [ ] DuckDB resolves query-visible names and fetches complete columns, nullability, Primary Keys, and Unique constraints for reachable external base tables.
- [ ] Repeated references to one canonical Relation fetch once while final IR still creates distinct Alias occurrences.
- [ ] Fetched views expand recursively through the existing parser and IR converter; recursive views terminate with diagnostics.
- [ ] The final IR is rebuilt before the fetcher binds metadata to direct `Knowledge` references.
- [ ] The engine accepts bound Knowledge as `Accepted` evidence; no analysis function does relation or column name lookup.
- [ ] A Unique Join against a live DuckDB primary key or composite/nullable Unique Set proves with current semantics.
- [ ] Candidate Key assertions use catalog uniqueness and non-nullability.
- [ ] Missing objects, permissions failures, and incomplete metadata produce diagnostics and no unsound proof.
- [ ] SQL-declared Relations are not fetched or overwritten.
- [ ] No connection, cursor, catalog row, qualified name, raw view SQL, or SQLGlot node enters Knowledge or engine evidence.
- [ ] Offline `analyze(sql, *, dialect=...)` stays pure.
- [ ] Tests cover generic recursion with a fake fetcher and end-to-end DuckDB hydration with an in-memory connection.
- [ ] The full test suite and `ty check sqlassert tests` pass.

## Out of Scope

- Fetching the whole database catalog.
- Data-backed duplicate checks or table-row scans.
- Partial, expression-based, disabled, or invalid unique indexes.
- Generic proof abduction, proof-directed catalog retries, cross-analysis metadata caching, and adapters other than DuckDB.
