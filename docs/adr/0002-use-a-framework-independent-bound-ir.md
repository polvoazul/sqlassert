# Use a framework-independent bound IR

Make the bound intermediate representation the integration boundary between SQL analysis and property reasoning. It consists of immutable Python values with stable source-provenance identifiers and contains no SQLGlot nodes, database connections, or Clingo symbols. Query structure and externally supplied knowledge are represented separately, and only the property engine converts them into Clingo facts, allowing parsing, discovery, reasoning, and reporting to evolve independently.
