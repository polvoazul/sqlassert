"""`--connect`: the warehouse connection dbt itself would open.

The profile and target dbt ran with name the warehouse; dbt's adapter opens
the connection, and the driver's own handle feeds the matching core
`DatabaseKnowledgeFetcher`. This leans on dbt's adapter API
(`RuntimeConfig.from_args`, `register_adapter`, `connection_named`), which
is internal and changes across dbt versions: any failure is a
`ConnectError` naming the Python route, `analyze_project(fetcher=...)`.

Only the DuckDB adapter is tested end to end (`tests/test_connect.py`). The
others hand their driver's DB-API connection to the fetcher written for it.
"""

from __future__ import annotations

import os
from argparse import Namespace
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlassert.discovery import DatabaseKnowledgeFetcher


class ConnectError(Exception):
    """dbt's connection could not be opened, or its adapter has no fetcher."""


def _duckdb(handle: Any) -> DatabaseKnowledgeFetcher:
    from sqlassert.discovery.duckdb import DuckDBKnowledgeFetcher

    # dbt-duckdb wraps the connection; its cursor is a DuckDB connection.
    return DuckDBKnowledgeFetcher(handle.cursor())


def _dbapi(module: str, name: str) -> Callable[[Any], DatabaseKnowledgeFetcher]:
    def fetcher(handle: Any) -> DatabaseKnowledgeFetcher:
        import importlib

        return getattr(importlib.import_module(f"sqlassert.discovery.{module}"), name)(handle)

    return fetcher


# dbt adapter type: the fetcher over its connection handle. BigQuery (a
# client, not a DB-API connection) and Databricks (a wrapped connection) are
# not mapped yet: pass a fetcher through `analyze_project` instead.
FETCHERS: dict[str, Callable[[Any], DatabaseKnowledgeFetcher]] = {
    "duckdb": _duckdb,
    "postgres": _dbapi("postgres", "PostgresKnowledgeFetcher"),
    "redshift": _dbapi("redshift", "RedshiftKnowledgeFetcher"),
    "snowflake": _dbapi("snowflake", "SnowflakeKnowledgeFetcher"),
    "mysql": _dbapi("mysql", "MySQLKnowledgeFetcher"),
    "trino": _dbapi("trino", "TrinoKnowledgeFetcher"),
}


@contextmanager
def dbt_fetcher(project: Path, dbt_arguments: list[str]) -> Generator[DatabaseKnowledgeFetcher]:
    """A fetcher over the connection dbt opens for `project` with the
    profile, target, profiles directory and vars in `dbt_arguments`, closed
    on exit: a DuckDB file takes one writer, so close it before dbt runs again."""
    try:
        import yaml
        from dbt.adapters.factory import cleanup_connections, get_adapter, register_adapter, reset_adapters
        from dbt.config.runtime import RuntimeConfig
        from dbt.flags import set_from_args
        from dbt.mp_context import get_mp_context
    except ImportError as error:
        raise ConnectError(f"--connect needs dbt installed in sqlassert-dbt's Python ({error})") from error
    try:
        args = Namespace(
            project_dir=str(project), profiles_dir=str(_profiles_dir(project, dbt_arguments)),
            profile=_option(dbt_arguments, "--profile"), target=_option(dbt_arguments, "--target", "-t"),
            vars=yaml.safe_load(_option(dbt_arguments, "--vars") or "{}") or {}, threads=None,
        )
        set_from_args(args, None)
        config = RuntimeConfig.from_args(args)
        # An adapter dbt registered earlier in this process (dbtRunner) stays
        # registered under its type, with that run's profile: replace it.
        reset_adapters()
        register_adapter(config, get_mp_context())
        adapter = get_adapter(config)
    except Exception as error:  # dbt's internals raise their own types, version by version.
        raise ConnectError(f"dbt could not load the profile for --connect: {error}") from error
    kind = adapter.type()
    if kind not in FETCHERS:
        raise ConnectError(
            f"--connect does not support the {kind} adapter yet: call analyze_project(..., fetcher=...) with a "
            f"sqlassert.discovery fetcher over your own connection"
        )
    try:
        with adapter.connection_named("sqlassert"):
            handle = adapter.connections.get_thread_connection().handle
            yield FETCHERS[kind](handle)
    finally:
        cleanup_connections()
        if kind == "duckdb":
            _close_duckdb_environment()


def _close_duckdb_environment() -> None:
    """dbt-duckdb keeps its database open past `cleanup_connections` (its
    environment's `keep_open`), holding the file's lock against the next dbt
    process; close it."""
    from dbt.adapters.duckdb.connections import DuckDBConnectionManager

    environment = DuckDBConnectionManager._ENV
    DuckDBConnectionManager.close_all_connections()
    if environment is not None:
        environment.close()


def _profiles_dir(project: Path, arguments: list[str]) -> Path:
    """Where dbt reads profiles.yml: --profiles-dir, DBT_PROFILES_DIR, the
    working directory, then ~/.dbt; and the project, as `_profiles` passes it."""
    given = _option(arguments, "--profiles-dir") or os.environ.get("DBT_PROFILES_DIR")
    if given:
        return Path(given)
    for folder in (Path.cwd(), project):
        if (folder / "profiles.yml").is_file():
            return folder
    return Path.home() / ".dbt"


def _option(arguments: list[str], *names: str) -> str | None:
    """The value given for any of `names` (`--name V` or `--name=V`)."""
    for index, argument in enumerate(arguments):
        for name in names:
            if argument == name and index + 1 < len(arguments):
                return arguments[index + 1]
            if argument.startswith(name + "="):
                return argument.split("=", 1)[1]
    return None
