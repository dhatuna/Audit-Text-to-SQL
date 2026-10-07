"""Layer B: the database itself refuses what the validator is supposed to stop.

These tests talk to PostgreSQL directly, bypassing the validator, to prove the second
line of defence holds on its own.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import errors

from auditable_sql.bootstrap.settings import Settings
from auditable_sql.infrastructure.context.semantic_layer import SemanticLayer

Conn = psycopg.Connection[Any]


def test_grants_match_the_semantic_layer_exactly(admin: Conn, settings: Settings) -> None:
    """The YAML whitelist and the database grants must never drift apart."""
    layer = SemanticLayer.load(settings.semantic_layer_path)
    expected = {
        (table, col)
        for table, cols in layer.policy(max_rows=1).allowed_columns.items()
        for col in cols
    }
    rows = admin.execute(
        """
        SELECT table_name, column_name FROM information_schema.column_privileges
        WHERE grantee = 'nl2sql_reader' AND privilege_type = 'SELECT'
        """
    ).fetchall()
    assert set(rows) == expected


def test_reader_has_no_table_level_privileges(admin: Conn) -> None:
    rows = admin.execute(
        "SELECT table_name, privilege_type FROM information_schema.table_privileges "
        "WHERE grantee = 'nl2sql_reader'"
    ).fetchall()
    assert rows == []


def test_reader_can_read_exposed_columns(reader: Conn) -> None:
    (n,) = reader.execute("SELECT COUNT(*) FROM positions_eod").fetchone()  # type: ignore[misc]
    assert n > 0
    reader.execute("SELECT client_id, risk_profile FROM clients").fetchall()


@pytest.mark.parametrize(
    "query",
    [
        "SELECT tax_id FROM clients",
        "SELECT * FROM clients",
        "SELECT c FROM clients c",  # whole-row reference
        "SELECT row_to_json(c) FROM clients c",
    ],
)
def test_reader_cannot_read_pii(reader: Conn, query: str) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        reader.execute(query)


def test_reader_cannot_read_audit_log(reader: Conn) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        reader.execute("SELECT * FROM audit.query_log")


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE trades SET fees = 0",
        "DELETE FROM trades",
        "INSERT INTO securities VALUES (9999, 'ZZZZ', 'x', 'etf', NULL)",
        "CREATE TABLE stolen (x int)",
        "CREATE TEMP TABLE stolen (x int)",
    ],
)
def test_reader_cannot_write(reader: Conn, query: str) -> None:
    # Read-only by role default, so this fails even before privileges are checked.
    with pytest.raises((errors.ReadOnlySqlTransaction, errors.InsufficientPrivilege)):
        reader.execute(query)


def test_reader_session_is_read_only_by_default(reader: Conn) -> None:
    (value,) = reader.execute("SHOW default_transaction_read_only").fetchone()  # type: ignore[misc]
    assert value == "on"


def test_reader_statement_timeout_is_enforced(reader: Conn) -> None:
    with pytest.raises(errors.QueryCanceled):
        reader.execute("SELECT pg_sleep(10)")


def test_reader_cannot_read_server_files(reader: Conn) -> None:
    with pytest.raises(errors.InsufficientPrivilege):
        reader.execute("SELECT pg_read_file('/etc/passwd')")


class TestAuditTableIsAppendOnly:
    def _insert(self, conn: Conn, rid: str) -> None:
        conn.execute(
            """
            INSERT INTO audit.query_log (request_id, created_at, user_label, question,
                context_version, context_sha256, context_text, prompt_version, outcome,
                attempts, total_latency_ms)
            VALUES (%s, now(), 'test', 'q', 'v', 'h', 't', 'p', 'refused', '[]', 1)
            """,
            (rid,),
        )

    def test_app_role_can_insert_and_read(self, app_conn: Conn) -> None:
        self._insert(app_conn, "perm-insert")
        assert app_conn.execute(
            "SELECT 1 FROM audit.query_log WHERE request_id = 'perm-insert'"
        ).fetchone()

    @pytest.mark.parametrize(
        "query",
        [
            "UPDATE audit.query_log SET question = 'rewritten'",
            "DELETE FROM audit.query_log",
            "TRUNCATE audit.query_log",
        ],
    )
    def test_app_role_cannot_rewrite_history(self, app_conn: Conn, query: str) -> None:
        self._insert(app_conn, f"perm-{abs(hash(query))}")
        with pytest.raises(errors.InsufficientPrivilege):
            app_conn.execute(query)

    @pytest.mark.parametrize(
        "query",
        [
            "UPDATE audit.query_log SET question = 'rewritten'",
            "DELETE FROM audit.query_log",
            "TRUNCATE audit.query_log",
        ],
    )
    def test_even_the_owner_cannot_rewrite_history(self, admin: Conn, query: str) -> None:
        self._insert(admin, f"owner-{abs(hash(query))}")
        with pytest.raises(errors.RaiseException, match="append-only"):
            admin.execute(query)

    def test_app_role_cannot_read_portfolio_data(self, app_conn: Conn) -> None:
        with pytest.raises(errors.InsufficientPrivilege):
            app_conn.execute("SELECT trade_id FROM public.trades")
