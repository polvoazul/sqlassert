"""Constraints added after `CREATE TABLE`: `ALTER TABLE ... ADD PRIMARY KEY /
UNIQUE / FOREIGN KEY`, `ALTER COLUMN ... SET NOT NULL`, and `CREATE UNIQUE
INDEX`. Each is the same Knowledge its `CREATE TABLE` form states. One that
names no table created before it is diagnosed, as is any other `ALTER`.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import CONSTRAINT_WITHOUT_TABLE, UNSUPPORTED_CREATE_STATEMENT, UNSUPPORTED_STATEMENT

TABLES = """
CREATE TABLE users (id INTEGER, email VARCHAR, org INTEGER, nick VARCHAR);
CREATE TABLE orgs (id INTEGER PRIMARY KEY);
CREATE TABLE sessions (user_id INTEGER NOT NULL);
"""


def outcomes(sql: str) -> list[Outcome]:
    report = analyze(sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


PROVED = [
    ("ADD PRIMARY KEY", "ALTER TABLE users ADD PRIMARY KEY (id);", "SELECT id FROM users /**ASSERT NOT NULL UNIQUE(id)**/"),
    ("named PRIMARY KEY", "ALTER TABLE users ADD CONSTRAINT pk PRIMARY KEY (id);", "SELECT id FROM users /**ASSERT NOT NULL UNIQUE(id)**/"),
    ("composite PRIMARY KEY", "ALTER TABLE users ADD PRIMARY KEY (org, email);", "SELECT org, email FROM users /**ASSERT NOT NULL UNIQUE(org, email)**/"),
    ("ADD UNIQUE", "ALTER TABLE users ADD UNIQUE (email);", "SELECT email FROM users /**ASSERT UNIQUE(email)**/"),
    ("named UNIQUE", "ALTER TABLE users ADD CONSTRAINT u UNIQUE (org, email);", "SELECT org, email FROM users /**ASSERT UNIQUE(org, email)**/"),
    ("SET NOT NULL", "ALTER TABLE users ALTER COLUMN nick SET NOT NULL;", "SELECT nick FROM users /**ASSERT NOT NULL(nick)**/"),
    ("UNIQUE plus SET NOT NULL", "ALTER TABLE users ALTER email SET NOT NULL; ALTER TABLE users ADD UNIQUE (email);", "SELECT email FROM users /**ASSERT NOT NULL UNIQUE(email)**/"),
    ("CREATE UNIQUE INDEX", "CREATE UNIQUE INDEX users_email ON users (email);", "SELECT email FROM users /**ASSERT UNIQUE(email)**/"),
    ("composite UNIQUE INDEX", "CREATE UNIQUE INDEX users_org_email ON users (org, email);", "SELECT org, email FROM users /**ASSERT UNIQUE(org, email)**/"),
    (
        "ADD FOREIGN KEY",
        "ALTER TABLE users ALTER COLUMN org SET NOT NULL; ALTER TABLE users ADD FOREIGN KEY (org) REFERENCES orgs (id);",
        "SELECT u.id FROM users u /**ASSERT EXACTLY ONE**/ JOIN orgs o ON u.org = o.id",
    ),
    (
        "ADD FOREIGN KEY onto the primary key",
        "ALTER TABLE users ALTER COLUMN org SET NOT NULL; ALTER TABLE users ADD CONSTRAINT fk FOREIGN KEY (org) REFERENCES orgs;",
        "SELECT u.id FROM users u /**ASSERT EXACTLY ONE**/ JOIN orgs o ON u.org = o.id",
    ),
    (
        "ADD PRIMARY KEY makes a join unique",
        "ALTER TABLE users ADD PRIMARY KEY (id);",
        "SELECT s.user_id FROM sessions s /**ASSERT UNIQUE**/ JOIN users u ON s.user_id = u.id",
    ),
]


@pytest.mark.parametrize(("label", "ddl", "query"), PROVED, ids=[label for label, _, _ in PROVED])
def test_a_constraint_added_after_create_table_is_knowledge(label: str, ddl: str, query: str):
    assert outcomes(f"{TABLES} {ddl} {query}") == [Outcome.PROVED]


def test_a_named_table_constraint_in_create_table_is_knowledge():
    sql = "CREATE TABLE t (a INTEGER, b INTEGER, CONSTRAINT pk PRIMARY KEY (a), CONSTRAINT u UNIQUE (b)); "
    assert outcomes(sql + "SELECT a, b FROM t /**ASSERT NOT NULL UNIQUE(a)**/ /**ASSERT UNIQUE(b)**/") == [Outcome.PROVED, Outcome.PROVED]


NOT_PROVED = [
    ("plain index", "CREATE INDEX users_email ON users (email);", "SELECT email FROM users /**ASSERT UNIQUE(email)**/"),
    ("UNIQUE is not NOT NULL", "ALTER TABLE users ADD UNIQUE (email);", "SELECT email FROM users /**ASSERT NOT NULL(email)**/"),
    ("constraint on another table", "ALTER TABLE orgs ADD UNIQUE (id);", "SELECT email FROM users /**ASSERT UNIQUE(email)**/"),
    ("composite UNIQUE is not per column", "ALTER TABLE users ADD UNIQUE (org, email);", "SELECT email FROM users /**ASSERT UNIQUE(email)**/"),
]


@pytest.mark.parametrize(("label", "ddl", "query"), NOT_PROVED, ids=[label for label, _, _ in NOT_PROVED])
def test_what_a_constraint_does_not_state_is_not_proved(label: str, ddl: str, query: str):
    assert outcomes(f"{TABLES} {ddl} {query}") == [Outcome.UNKNOWN]


@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER TABLE users ADD UNIQUE (email); CREATE TABLE users (id INTEGER, email VARCHAR);",
        "CREATE UNIQUE INDEX users_email ON users (email); CREATE TABLE users (id INTEGER, email VARCHAR);",
        "ALTER TABLE users ADD UNIQUE (email);",
        "CREATE VIEW users AS SELECT 1 AS email UNION ALL SELECT 1; ALTER TABLE users ADD UNIQUE (email);",
    ],
    ids=["ALTER before CREATE", "INDEX before CREATE", "never created", "a view"],
)
def test_a_constraint_on_a_table_not_created_before_it_is_diagnosed(ddl: str):
    report = analyze(f"{ddl} SELECT email FROM users /**ASSERT UNIQUE(email)**/")
    assert [diagnostic.code for diagnostic in report.diagnostics] == [CONSTRAINT_WITHOUT_TABLE]
    assert "users" in report.diagnostics[0].message
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE UNIQUE INDEX users_email ON users (lower(email));",
        "CREATE UNIQUE INDEX users_email ON users (email) WHERE org > 0;",
    ],
    ids=["expression", "partial"],
)
def test_a_unique_index_not_over_plain_columns_is_diagnosed_and_states_nothing(ddl: str):
    report = analyze(f"{TABLES} {ddl} SELECT email FROM users /**ASSERT UNIQUE(email)**/")
    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNSUPPORTED_CREATE_STATEMENT]
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


@pytest.mark.parametrize(
    "ddl",
    [
        "ALTER TABLE users ADD COLUMN extra INTEGER;",
        "ALTER TABLE users ALTER COLUMN email DROP NOT NULL;",
        "ALTER TABLE users ADD UNIQUE (email), DROP COLUMN nick;",
        "ALTER TABLE users RENAME TO people;",
    ],
    ids=["ADD COLUMN", "DROP NOT NULL", "mixed with an unsupported action", "RENAME"],
)
def test_any_other_alter_table_is_diagnosed_and_states_nothing(ddl: str):
    report = analyze(f"{TABLES} {ddl} SELECT email FROM users /**ASSERT UNIQUE(email)**/")
    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNSUPPORTED_STATEMENT]
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
