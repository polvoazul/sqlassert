"""Python analysis functions establish public relational properties.

Equality groups say which columns carry one value (equality), for facts
about values.

Null sources say why a column may be NULL (nullability). Functional
dependencies and their closure decide uniqueness: a column set is a Unique
Set when its closure reaches the relation's Row Identity, and a join is
many to one when the right columns ON binds reach the right input's
(uniqueness, join_cardinality). Foreign Keys and Exactly One Joins build on both.
"""
