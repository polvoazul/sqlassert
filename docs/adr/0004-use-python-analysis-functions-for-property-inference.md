# Use Python analysis functions for property inference

Use Python analysis functions over the semantic IR as the default property engine. Each function returns `Result[Value]`: the computed value plus directly represented evidence; public functions evaluate declarable Properties by naming convention, while underscore-prefixed functions derive internal values. This makes explanation native to inference and keeps rule concepts in Python rather than transporting them through Clingo. The preserved Clingo engine remains a parity implementation. This supersedes ADR-0001.
