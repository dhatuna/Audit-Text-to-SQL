"""Clean Architecture dependency rule as a test, so `pytest` alone catches a violation.

Contracts live in pyproject.toml ([tool.importlinter]).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from importlinter.cli import lint_imports

ROOT = Path(__file__).resolve().parents[2]


def test_import_contracts_hold(capsys: pytest.CaptureFixture[str]) -> None:
    cwd = Path.cwd()
    os.chdir(ROOT)
    try:
        exit_code = lint_imports(no_cache=True)
    finally:
        os.chdir(cwd)
    out = capsys.readouterr().out
    assert "Contracts: 3 kept, 0 broken." in out, out
    assert exit_code == 0, out
