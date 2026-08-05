import pytest

import schema_inference.llm.registry as registry
from schema_inference.llm.providers.anthropic_provider import AnthropicProvider
from schema_inference.llm.providers.openai_provider import OpenAIProvider


@pytest.fixture(autouse=True)
def _clear_provider_cache():
    """get_provider() caches instances process-wide, keyed by resolved
    provider name (registry._build_provider is an lru_cache) -- clear it
    before and after each test so one test's monkeypatched config doesn't
    leak a stale cached instance into the next."""
    registry._build_provider.cache_clear()
    yield
    registry._build_provider.cache_clear()


# ── load_llm_config: graceful degradation ──────────────────────────────────

def test_load_llm_config_returns_empty_dict_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", tmp_path / "does_not_exist.yml")
    assert registry.load_llm_config() == {}


def test_load_llm_config_returns_empty_dict_on_malformed_yaml(monkeypatch, tmp_path):
    bad = tmp_path / "agent_config.yml"
    bad.write_text("llm: [unterminated", encoding="utf-8")
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", bad)
    assert registry.load_llm_config() == {}


def test_load_llm_config_returns_empty_dict_when_llm_section_absent(monkeypatch, tmp_path):
    cfg = tmp_path / "agent_config.yml"
    cfg.write_text("rule_engine:\n  weights:\n    name_sim: 0.5\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", cfg)
    assert registry.load_llm_config() == {}


def test_load_llm_config_reads_the_llm_section(monkeypatch, tmp_path):
    cfg = tmp_path / "agent_config.yml"
    cfg.write_text(
        "llm:\n  provider: openai\n  models:\n    mapping_agent: gpt-4o-mini\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", cfg)
    assert registry.load_llm_config() == {
        "provider": "openai",
        "models": {"mapping_agent": "gpt-4o-mini"},
    }


# ── model_for(): config override with hardcoded fallback ───────────────────

def test_model_for_falls_back_to_hardcoded_defaults_when_config_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", tmp_path / "missing.yml")
    assert registry.model_for("mapping_agent") == "claude-haiku-4-5-20251001"
    assert registry.model_for("critic_agent") == "claude-sonnet-4-6"
    assert registry.model_for("sql_agent") == "claude-haiku-4-5-20251001"
    assert registry.model_for("tune_prompts") == "claude-sonnet-4-6"


def test_model_for_falls_back_for_an_unrecognized_agent_key(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", tmp_path / "missing.yml")
    assert registry.model_for("some_future_agent") == registry._DEFAULT_MODELS["mapping_agent"]


def test_model_for_reads_a_config_override_and_leaves_others_at_default(monkeypatch, tmp_path):
    cfg = tmp_path / "agent_config.yml"
    cfg.write_text("llm:\n  models:\n    critic_agent: claude-opus-4-8\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", cfg)
    assert registry.model_for("critic_agent") == "claude-opus-4-8"
    assert registry.model_for("mapping_agent") == "claude-haiku-4-5-20251001"


# ── get_provider(): provider selection, graceful fallback, caching ─────────

def test_get_provider_defaults_to_anthropic_when_config_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", tmp_path / "missing.yml")
    assert isinstance(registry.get_provider(), AnthropicProvider)


def test_get_provider_returns_openai_when_configured(monkeypatch, tmp_path):
    cfg = tmp_path / "agent_config.yml"
    cfg.write_text("llm:\n  provider: openai\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", cfg)
    assert isinstance(registry.get_provider(), OpenAIProvider)


def test_get_provider_falls_back_to_anthropic_for_an_unrecognized_provider_name(monkeypatch, tmp_path):
    """A config typo must degrade to the known-good default, not crash the
    pipeline -- same graceful-degradation contract as _rule_weights()."""
    cfg = tmp_path / "agent_config.yml"
    cfg.write_text("llm:\n  provider: not_a_real_provider\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", cfg)
    assert isinstance(registry.get_provider(), AnthropicProvider)


def test_get_provider_caches_the_same_instance_across_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", tmp_path / "missing.yml")
    p1 = registry.get_provider()
    p2 = registry.get_provider("mapping_agent")
    p3 = registry.get_provider("critic_agent")
    assert p1 is p2 is p3


def test_get_provider_passes_provider_specific_config_to_the_adapter(monkeypatch, tmp_path):
    cfg = tmp_path / "agent_config.yml"
    cfg.write_text(
        "llm:\n  provider: anthropic\n  providers:\n    anthropic:\n      api_key_env: MY_CUSTOM_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", cfg)
    provider = registry.get_provider()
    assert isinstance(provider, AnthropicProvider)
    assert provider._config == {"api_key_env": "MY_CUSTOM_KEY"}
