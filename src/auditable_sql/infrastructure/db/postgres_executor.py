"""Runs validated SQL as the read-only role, inside a read-only transaction with a timeout."""

from __future__ import annotations

import time
from typing import cast

import psycopg
from psycopg import sql

from auditable_sql.domain.errors import QueryExecutionError
from auditable_sql.domain.models import Cell, QueryResult


class PostgresExecutor:
    def __init__(self, dsn: str, *, max_rows: int, statement_timeout_ms: int) -> None:
        self._dsn = dsn
        self._max_rows = max_rows
        self._timeout_ms = statement_timeout_ms

    def execute(self, query: str) -> QueryResult:
        try:
            with psycopg.connect(self._dsn, autocommit=False, connect_timeout=5) as conn:
                conn.read_only = True  # BEGIN READ ONLY, on top of the role default
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("SET LOCAL statement_timeout = {}").format(
                            sql.Literal(f"{self._timeout_ms}ms")
                        )
                    )
                    started = time.perf_counter()
                    # The text has passed SqlglotValidator; it is not interpolated with
                    # anything, and runs under a role that can only read exposed columns.
                    cur.execute(query)
                    rows = cur.fetchmany(self._max_rows + 1)
                    elapsed = (time.perf_counter() - started) * 1000
                    columns = tuple(d.name for d in cur.description or ())
                conn.rollback()
        except psycopg.Error as exc:
            diag = getattr(exc, "diag", None)
            message = (diag.message_primary if diag else None) or str(exc).strip()
            sqlstate = getattr(exc, "sqlstate", None)
            raise QueryExecutionError(f"[{sqlstate}] {message}" if sqlstate else message) from exc

        truncated = len(rows) > self._max_rows
        return QueryResult(
            columns=columns,
            rows=tuple(tuple(cast(Cell, c) for c in r) for r in rows[: self._max_rows]),
            truncated=truncated,
            elapsed_ms=elapsed,
        )
