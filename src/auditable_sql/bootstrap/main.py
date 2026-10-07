"""Entry point for the `asql` command."""

from __future__ import annotations

from auditable_sql.bootstrap.container import Container
from auditable_sql.interfaces.cli import build_app


def main() -> None:
    build_app(Container().services())()


if __name__ == "__main__":
    main()
