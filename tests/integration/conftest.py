"""A throwaway PostgreSQL with the real init scripts (schema, audit table, roles) and a
small seeded dataset. Requires Docker; tests here are marked `integration`."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr
from testcontainers.community.postgres import PostgresContainer

from auditable_sql.bootstrap.settings import Settings
from auditable_sql.infrastructure.db.seed import Dataset, Scale, generate, load

ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def dataset() -> Dataset:
    return generate(seed=11, scale=Scale.small())


@pytest.fixture(scope="session")
def settings(dataset: Dataset) -> Iterator[Settings]:
    container = (
        PostgresContainer(
            "postgres:16-alpine", username="admin", password="admin-pw", dbname="portfolio"
        )
        .with_env("READER_PASSWORD", "reader-pw")
        .with_env("APP_PASSWORD", "app-pw")
        .with_volume_mapping(str(ROOT / "db" / "init"), "/docker-entrypoint-initdb.d", "ro")
    )
    with container:
        s = Settings(
            db_host=container.get_container_host_ip(),
            db_port=int(container.get_exposed_port(5432)),
            db_name="portfolio",
            admin_user="admin",
            admin_password=SecretStr("admin-pw"),
            reader_password=SecretStr("reader-pw"),
            app_password=SecretStr("app-pw"),
            statement_timeout_ms=1_000,
            max_rows=50,
        )
        with psycopg.connect(s.admin_dsn) as conn:
            load(conn, dataset)
        yield s


@pytest.fixture
def admin(settings: Settings) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def reader(settings: Settings) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(settings.reader_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def app_conn(settings: Settings) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(settings.app_dsn, autocommit=True) as conn:
        yield conn
