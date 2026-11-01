"""Python analysis functions establish public relational properties.

The rules derive and propagate unique sets, generalize uniqueness to column
sets of interest, establish unique joins from right-side key coverage, and
follow non-null columns to their sources; non-null unique sets combine the two.
Assertions register sets of interest; reporting checks public properties.
"""
