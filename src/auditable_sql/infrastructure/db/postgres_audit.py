"""Append-only audit repository on audit.query_log (role: nl2sql_app, INSERT/SELECT only)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from auditable_sql.domain.errors import AuditWriteError
from auditable_sql.domain.models import AuditRecord
from auditable_sql.infrastructure.db.audit_codec import COLUMNS, from_row, to_row

_JSON_COLUMNS = {"attempts", "answer", "grounding"}
_TABLE = sql.Identifier("audit", "query_log")
_SELECT = sql.SQL("SELECT {} FROM {}").format(
    sql.SQL(", ").join(map(sql.Identifier, COLUMNS)), _TABLE
)


class PostgresAuditRepository:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def save(self, record: AuditRecord) -> None:
        row = to_row(record)
        params: dict[str, Any] = {
            k: Jsonb(v) if k in _JSON_COLUMNS and v is not None else v for k, v in row.items()
        }
        stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            _TABLE,
            sql.SQL(", ").join(map(sql.Identifier, COLUMNS)),
            sql.SQL(", ").join(sql.Placeholder(c) for c in COLUMNS),
        )
        try:
            with psycopg.connect(self._dsn, connect_timeout=5) as conn:
                conn.execute(stmt, params)
        except psycopg.Error as exc:
            raise AuditWriteError(str(exc).strip()) from exc

    def get(self, request_id: str) -> AuditRecord | None:
        with (
            psycopg.connect(self._dsn, connect_timeout=5) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute(_SELECT + sql.SQL(" WHERE request_id = %s"), (request_id,))
            row = cur.fetchone()
        return from_row(row) if row else None

    def recent(self, limit: int = 20) -> Sequence[AuditRecord]:
        with (
            psycopg.connect(self._dsn, connect_timeout=5) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute(_SELECT + sql.SQL(" ORDER BY created_at DESC LIMIT %s"), (limit,))
            return [from_row(r) for r in cur.fetchall()]
