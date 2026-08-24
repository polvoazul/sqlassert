# Isolate Clingo as the property engine

Use Clingo only to derive properties and their supporting evidence from normalized semantic facts. SQL parsing, name resolution, catalog access, and source provenance remain outside the solver. For the MVP, a `Reporter` instance consumes the live `clingo.Model` through its bound `on_model` callback and stores only the durable report; this deliberate reporting-to-Clingo coupling avoids an intermediate evidence model until that coupling causes a concrete problem.
