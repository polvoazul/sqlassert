"""The names of a derived table's columns, when some repeat.

DuckDB renames the repeated output names of a derived table (a subquery, CTE,
view or aliased join): `SELECT * FROM (SELECT a.k, b.k, b.k_1 ...) s` has the
columns `k, k_1, k_1_1`, so `s.k_1` is `b.k` and `b.k_1` is `s.k_1_1`. Another
engine rejects the name or generates others. A name a user writes after the
first of a repeated one therefore says nothing about which column it is.

`derived_names` gives each column after the first of a name an internal name
(`__duplicate_column_k_1`) that no reference writes, and `may_be_renamed` says
which names a reference to such a table cannot be trusted to bind: every
`<repeated>_<digits>`. The internal name is what carries the fact on, through
a star's expansion into the next relation.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from sqlassert.ir.model import OutputColumn

DUPLICATE_PREFIX = "__duplicate_column_"
_INTERNAL = re.compile(re.escape(DUPLICATE_PREFIX) + r"(.+)_[0-9]+", re.DOTALL)


def internal_base(name: str) -> str | None:
    """The lower-case name `name`, an internal one, stands for a repeat of; None for any other."""
    match = _INTERNAL.fullmatch(name)
    return None if match is None else match.group(1).lower()


def repeated_names(names: Iterable[str]) -> frozenset[str]:
    """The lower-case names `names` repeat, or already hold an internal name for."""
    names = list(names)
    counts = Counter(name.lower() for name in names)
    return frozenset(
        {name for name, count in counts.items() if count > 1}
        | {base for name in names if (base := internal_base(name)) is not None}
    )


def may_be_renamed(repeated: Iterable[str], name: str) -> bool:
    """Whether `name` is of the form `<repeated>_<digits>...`: one an engine may
    give a repeat of `repeated` (`k_1`, `k_2`, or `k_1_1` when `k_1` was taken;
    SQLite's is `k:1`)."""
    return any(re.fullmatch(re.escape(base) + r"([_:][0-9]+)+", name.lower()) for base in repeated)


def derived_names(names: Sequence[str]) -> list[str]:
    """`names` as a derived table names its columns: the first of a name keeps
    it, each repeat has an internal name that no reference writes."""
    seen: set[str] = set()
    counts: Counter[str] = Counter()
    derived: list[str] = []
    for name in names:
        base = internal_base(name)
        if name.lower() not in seen:
            derived.append(name)
        else:
            base = base or name.lower()
            while True:
                counts[base] += 1
                candidate = f"{DUPLICATE_PREFIX}{base}_{counts[base]}"
                if candidate.lower() not in seen:
                    break
            derived.append(candidate)
        seen.add(derived[-1].lower())
    return derived


def nameable(columns: Sequence[OutputColumn]) -> tuple[OutputColumn, ...]:
    """The columns of one relation a marker can name unambiguously: named, not
    a name the relation repeats (the first of a repeat included), not an
    internal name, and not one an engine may have given a repeat."""
    repeated = repeated_names(column.name for column in columns)
    return tuple(
        column for column in columns
        if column.name and column.name.lower() not in repeated and internal_base(column.name) is None
        and not may_be_renamed(repeated, column.name)
    )
