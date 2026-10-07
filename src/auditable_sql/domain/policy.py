"""Access policy for generated SQL: what may be read, and how much."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# Functions that can read the filesystem, sleep, change session state, reach other
# databases, or make results non-reproducible. Non-determinism matters here because
# audit replay must re-execute stored SQL and get the same rows.
DEFAULT_DENIED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # side effects / escape hatches
        "pg_sleep", "pg_sleep_for", "pg_sleep_until",
        "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
        "lo_import", "lo_export", "lo_get", "lo_open",
        "dblink", "dblink_exec",
        "set_config", "current_setting",
        "pg_terminate_backend", "pg_cancel_backend",
        "query_to_xml", "query_to_json", "xpath",
        # non-deterministic: break replay
        "now", "current_date", "current_time", "current_timestamp",
        "localtime", "localtimestamp", "clock_timestamp", "statement_timestamp",
        "transaction_timestamp", "timeofday", "random", "rand", "gen_random_uuid",
    }
)  # fmt: skip

DEFAULT_DENIED_SCHEMAS: frozenset[str] = frozenset({"pg_catalog", "information_schema", "audit"})


@dataclass(frozen=True, slots=True)
class SqlPolicy:
    # table -> readable columns. Tables not listed are not readable at all.
    allowed_columns: Mapping[str, frozenset[str]]
    max_rows: int = 500
    denied_functions: frozenset[str] = DEFAULT_DENIED_FUNCTIONS
    denied_schemas: frozenset[str] = DEFAULT_DENIED_SCHEMAS
    default_schema: str = "public"

    def __post_init__(self) -> None:
        if self.max_rows <= 0:
            raise ValueError("max_rows must be positive")
        if not self.allowed_columns:
            raise ValueError("policy must allow at least one table")

    def is_table_allowed(self, table: str) -> bool:
        return table.lower() in self.allowed_columns

    def is_column_allowed(self, table: str, column: str) -> bool:
        cols = self.allowed_columns.get(table.lower())
        return cols is not None and column.lower() in cols

    def is_function_denied(self, name: str) -> bool:
        return name.lower() in self.denied_functions


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    statement_timeout_ms: int = 5_000
    # Extra generate->validate->execute cycles after the first one fails.
    max_repair_attempts: int = 1

    def __post_init__(self) -> None:
        if self.statement_timeout_ms <= 0:
            raise ValueError("statement_timeout_ms must be positive")
        if self.max_repair_attempts < 0:
            raise ValueError("max_repair_attempts must be >= 0")
