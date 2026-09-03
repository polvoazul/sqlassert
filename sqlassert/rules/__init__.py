"""Clingo rules establish and explain relational properties.

## Main objectives

The engine fundamentally needs to answer these questions:

1. Is an arbitrary column set unique?
   A **Unique Set** is a set of **Output Columns**; each **Output Column** already
   belongs to its **Relation Expression**. The arbitrary set is unique when it
   contains every member of at least one known **Unique Set**; it may contain
   additional **Output Columns**.
   - Is a **Unique Join Assertion** true? Its right-side constrained **Output
     Columns** form an arbitrary column set subject to the same question.
2. Which **Output Columns** are known non-null?
3. Is an arbitrary column set non-null unique?
   It must be unique, and every **Output Column** in it must be known non-null.
"""
