"""MAP-8 call-site wiring coverage — PR #3 review item 4.

The provider/registry layers (schema_inference/llm/) already have solid
unit coverage in isolation (test_llm_registry.py, test_llm_anthropic_provider.py,
test_llm_openai_provider.py). What's missing is proof that each agent module
actually *uses* that wiring correctly: that it asks the registry for the
right agent_key, and that the resolved model ID reaches provider.complete().
A regression here (e.g. a typo'd agent_key, or a hardcoded model string
creeping back in) wouldn't be caught by the provider-layer tests alone.

Each test below monkeypatches only `get_provider`/`model_for` in the target
agent module's own namespace (they're imported names, so patching the
provider/registry module itself wouldn't be seen) with a fake provider that
records the kwargs it received — no real network call, no API key needed.
"""

from __future__ import annotations

import pytest

import schema_inference.agents.critic_agent as critic_agent
import schema_inference.agents.mapping_agent as mapping_agent
import schema_inference.agents.sql_agent as sql_agent
from schema_inference.llm.types import LLMResponse
from schema_inference.models import ColumnMapping, ColumnProfile


@pytest.fixture(autouse=True)
def _disable_throttle(monkeypatch):
    # These tests call call_with_retry()/acall_with_retry() for real (with a
    # fake provider standing in for the network call) -- disable the
    # process-wide pacer so three fast unit tests don't pay real wall-clock
    # rate-limit spacing. See throttle.py's _DISABLE_ENV_VAR docstring:
    # "tests only, never for real runs."
    monkeypatch.setenv("SCHEMA_INFERENCE_DISABLE_THROTTLE", "1")


class _FakeProvider:
    """Records every complete() call's kwargs; returns a fixed text answer."""

    def __init__(self, response_text: str):
        self._response_text = response_text
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(content=[{"type": "text", "text": self._response_text}], stop_reason="end_turn")


def _profile(name="POL_NO", **overrides) -> ColumnProfile:
    defaults = dict(
        name=name, inferred_type="string", null_rate=0.0, distinct_count=5,
        sample_values=["1", "2", "3"], value_distribution={},
    )
    defaults.update(overrides)
    return ColumnProfile(**defaults)


# ── sql_agent.py ─────────────────────────────────────────────────────────

def test_sql_agent_asks_registry_for_sql_agent_key_and_passes_its_model(monkeypatch):
    fake = _FakeProvider('{"expressions": [{"source_column": "POL_NO", "sql_expression": "POL_NO"}]}')
    calls_to_get_provider = []

    def fake_get_provider(agent_key):
        calls_to_get_provider.append(agent_key)
        return fake

    monkeypatch.setattr(sql_agent, "get_provider", fake_get_provider)
    monkeypatch.setattr(sql_agent, "_model", lambda: "configured-sql-model")

    mapping = ColumnMapping(
        source_column="POL_NO", source_table="t", target_field="policy_id",
        confidence=0.9, method="critic", sql_expression="POL_NO", notes="",
    )
    sql_agent.run_sql_agent([mapping], {"POL_NO": _profile()})

    assert calls_to_get_provider == ["sql_agent"]
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "configured-sql-model"


# ── critic_agent.py ──────────────────────────────────────────────────────

def test_critic_agent_asks_registry_for_critic_agent_key_and_passes_its_model(monkeypatch):
    fake = _FakeProvider('{"reviews": []}')
    calls_to_get_provider = []

    def fake_get_provider(agent_key):
        calls_to_get_provider.append(agent_key)
        return fake

    monkeypatch.setattr(critic_agent, "get_provider", fake_get_provider)
    monkeypatch.setattr(critic_agent, "_model", lambda: "configured-critic-model")
    # Force one column into the review targets without depending on a real
    # ground-truth catalog file on disk.
    monkeypatch.setattr(critic_agent, "_load_catalog_notes", lambda source_name: {"POL_NO": {"is_hard": True}})

    mapping = ColumnMapping(
        source_column="POL_NO", source_table="t", target_field="policy_id",
        confidence=0.5, method="rule", sql_expression="POL_NO", notes="",
    )
    critic_agent.run_critic_agent([mapping], {"POL_NO": _profile()}, source_name="pasl")

    assert calls_to_get_provider == ["critic_agent"]
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "configured-critic-model"


# ── mapping_agent.py ─────────────────────────────────────────────────────

def test_mapping_agent_asks_registry_for_mapping_agent_key_and_passes_its_model(monkeypatch):
    fake = _FakeProvider('{"target_field": "policy_id", "confidence": 0.8, "reasoning": "matches"}')
    calls_to_get_provider = []

    def fake_get_provider(agent_key):
        calls_to_get_provider.append(agent_key)
        return fake

    monkeypatch.setattr(mapping_agent, "get_provider", fake_get_provider)
    monkeypatch.setattr(mapping_agent, "_model", lambda: "configured-mapping-model")

    col = _profile()
    mapping_agent.run_mapping_agent(
        columns=[col], source_table="pasl_policy", source_name="pasl",
        all_profiles=[col], concurrency=1,
    )

    assert calls_to_get_provider == ["mapping_agent"]
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "configured-mapping-model"
