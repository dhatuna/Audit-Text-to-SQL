"""Composition root wiring. Builds real objects; makes no network or database calls."""

from __future__ import annotations

import pytest

from auditable_sql.bootstrap.container import Container, Variant
from auditable_sql.bootstrap.settings import Settings
from auditable_sql.infrastructure.context.semantic_layer import RawDdlContext, SemanticLayerContext
from auditable_sql.infrastructure.llm.anthropic_adapters import ClaudeSqlGenerator


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> Container:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    return Container(Settings(max_repair_attempts=2, model="claude-opus-5-5", effort="high"))


def test_improved_variant(container: Container) -> None:
    ask = container.ask(Variant.IMPROVED)
    assert isinstance(ask._context_provider, SemanticLayerContext)
    assert ask._limits.max_repair_attempts == 2
    gen = ask._generator
    assert isinstance(gen, ClaudeSqlGenerator)
    assert (gen._model, gen._effort, gen._use_fallbacks) == ("claude-opus-5-5", "high", True)


def test_baseline_variant_has_no_semantic_layer_and_no_repair(container: Container) -> None:
    ask = container.ask(Variant.BASELINE)
    assert isinstance(ask._context_provider, RawDdlContext)
    assert ask._limits.max_repair_attempts == 0


def test_both_variants_share_the_same_guardrails(container: Container) -> None:
    improved, baseline = container.ask(Variant.IMPROVED), container.ask(Variant.BASELINE)
    assert improved._validator is baseline._validator is container.validator
    assert improved._executor is baseline._executor


def test_services_bundle(container: Container) -> None:
    services = container.services()
    assert services.variants == ("improved", "baseline")
    assert isinstance(services.ask(True)._context_provider, RawDdlContext)
    assert services.model == "claude-opus-5-5"
    assert services.replay() is not None and services.check_gold() is not None


def test_dsns_use_separate_roles() -> None:
    s = Settings()
    assert "user=nl2sql_reader" in s.reader_dsn
    assert "user=nl2sql_app" in s.app_dsn
    assert "user=portfolio_admin" in s.admin_dsn
    assert "reader-local-only" not in repr(s)  # secrets are masked


def test_api_key_from_settings_reaches_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Settings(ANTHROPIC_API_KEY="sk-from-dotenv")  # type: ignore[call-arg]
    gen = Container(s).ask(Variant.IMPROVED)._generator
    assert isinstance(gen, ClaudeSqlGenerator)
    assert gen._client.api_key == "sk-from-dotenv"
    assert "sk-from-dotenv" not in repr(s)
