from __future__ import annotations

from pathlib import Path

import pytest

from auditable_sql.infrastructure.context.semantic_layer import (
    RawDdlContext,
    SemanticLayer,
    SemanticLayerContext,
)

ROOT = Path(__file__).resolve().parents[2]
HIDDEN = ("full_name", "email", "tax_id")


@pytest.fixture(scope="module")
def layer() -> SemanticLayer:
    return SemanticLayer.load(ROOT / "config" / "semantic_layer.yaml")


def test_hidden_columns_never_reach_the_model(layer: SemanticLayer) -> None:
    # Column definitions render as "- name type" (context) and "  name type" (DDL). The prose
    # may still say that emails are unavailable; it must never list them as columns.
    full_lines = layer.render().splitlines()
    ddl_lines = layer.render_ddl().splitlines()
    for col in HIDDEN:
        assert not any(line.startswith(f"- {col} ") for line in full_lines)
        assert not any(line.strip().startswith(f"{col} ") for line in ddl_lines)
    assert full_lines[full_lines.index("### clients") + 3].startswith("- risk_profile text")


def test_hidden_columns_are_not_in_the_policy(layer: SemanticLayer) -> None:
    policy = layer.policy(max_rows=10)
    assert policy.allowed_columns["clients"] == frozenset(
        {"client_id", "risk_profile", "onboarded_on"}
    )
    assert policy.max_rows == 10


def test_full_context_carries_definitions_and_gaps(layer: SemanticLayer) -> None:
    text = layer.render()
    assert "Never use CURRENT_DATE" in text
    assert "## Not available" in text and "Realized P&L" in text
    assert "positions_eod.account_id = accounts.account_id" in text
    assert "2024-01-02 to 2025-12-31" in text


def test_ddl_baseline_is_bare(layer: SemanticLayer) -> None:
    ddl = layer.render_ddl()
    assert ddl.startswith("CREATE TABLE clients (")
    assert "Business definitions" not in ddl and "--" not in ddl


def test_context_versions_and_hashes_differ_by_variant(layer: SemanticLayer) -> None:
    full, ddl = SemanticLayerContext(layer).get(), RawDdlContext(layer).get()
    assert full.version == f"semantic:{layer.version}" and ddl.version == f"ddl:{layer.version}"
    assert full.sha256 != ddl.sha256 and len(full.sha256) == 64


def test_context_hash_is_stable(layer: SemanticLayer) -> None:
    assert SemanticLayerContext(layer).get() == SemanticLayerContext(layer).get()


def test_tables_without_exposed_columns_are_dropped() -> None:
    layer = SemanticLayer(
        {
            "version": "t",
            "data_coverage": {"first_date": "a", "last_date": "b", "calendar": "c"},
            "tables": {
                "secret": {"columns": {"x": {"type": "int", "exposed": False}}},
                "open": {"columns": {"y": {"type": "int"}}},
            },
        }
    )
    assert set(layer.policy(5).allowed_columns) == {"open"}
    assert "secret" not in layer.render() and "secret" not in layer.render_ddl()
