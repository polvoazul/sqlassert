# Isolate Clingo as the property engine

Use Clingo to derive properties from normalized semantic facts. SQL parsing, name resolution, catalog access, and source locations remain outside the solver. A `Reporter` instance consumes public properties from the live `clingo.Model` through its bound `on_model` callback and stores plain values for the durable report. Proof explanations are deferred until a standard Clingo annotation interface and Python consumer are designed.
