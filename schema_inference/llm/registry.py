"""get_provider() / model_for() — resolve the configured LLM backend.

Reads agent_config.yml's `llm:` section through its own small loader rather
than importing agents/orchestrator.py's load_agent_config() — mirrors
mapper.py's independent `_rule_weights()` loader rather than throttle.py's
lazy cross-import, since schema_inference/llm/ sits below agents/ in the
dependency graph (agents depend on llm, not the reverse) and should stay
that way. Same graceful-degradation contract as the rest of the repo's
config loaders (_rule_weights(), _active_system_prompt(), load_agent_config()
itself): a missing or partial agent_config.yml never raises, it just runs
with the hardcoded Anthropic defaults below.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .provider import LLMProvider

_AGENT_CONFIG_PATH = Path(__file__).parent.parent / "agent_config.yml"

DEFAULT_PROVIDER = "anthropic"

# Fallback defaults — identical to the MODEL constants each agent module
# hardcoded before MAP-8. Only used when agent_config.yml is missing,
# malformed, or doesn't cover a given agent_key.
_DEFAULT_MODELS = {
    "mapping_agent": "claude-haiku-4-5-20251001",
    "critic_agent": "claude-sonnet-4-6",
    "sql_agent": "claude-haiku-4-5-20251001",
    "tune_prompts": "claude-sonnet-4-6",
}


def load_llm_config() -> dict[str, Any]:
    """Read agent_config.yml's `llm:` section. {} on any failure (missing
    file, malformed YAML, missing section) — every caller below already has
    a hardcoded fallback for that case."""
    if not _AGENT_CONFIG_PATH.exists():
        return {}
    try:
        with open(_AGENT_CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("llm") or {}
    except (yaml.YAMLError, OSError, ValueError, TypeError):
        return {}


def model_for(agent_key: str) -> str:
    """The configured model ID for one agent/tool (`mapping_agent`,
    `critic_agent`, `sql_agent`, `tune_prompts`), read from
    `llm.models.<agent_key>` in agent_config.yml. Falls back to the
    hardcoded default (today's per-module MODEL constant) if the config is
    missing, partial, or doesn't cover this key — same pattern as
    mapper.py's _rule_weights() / mapping_agent.py's _max_tool_calls()."""
    models = load_llm_config().get("models") or {}
    resolved = models.get(agent_key)
    if resolved:
        return str(resolved)
    return _DEFAULT_MODELS.get(agent_key, _DEFAULT_MODELS["mapping_agent"])


@lru_cache(maxsize=None)
def _build_provider(name: str) -> LLMProvider:
    """Construct (and cache, process-wide, keyed by provider name) one
    LLMProvider instance. Any provider name other than a recognized one
    degrades to the Anthropic default rather than raising — a config typo
    should not crash the pipeline."""
    provider_cfg = (load_llm_config().get("providers") or {}).get(name) or {}

    if name == "openai":
        from .providers.openai_provider import OpenAIProvider
        return OpenAIProvider(config=provider_cfg)

    from .providers.anthropic_provider import AnthropicProvider
    return AnthropicProvider(config=provider_cfg)


def get_provider(agent_key: str | None = None) -> LLMProvider:
    """Return the configured LLMProvider instance, cached process-wide.

    Reads `llm.provider` from agent_config.yml (falling back to
    `anthropic`) and returns a cached adapter instance for it.

    `agent_key` (e.g. "mapping_agent", "critic_agent", "sql_agent",
    "tune_prompts") is accepted for forward compatibility with a possible
    future per-agent provider override; today every agent shares the one
    provider named by `llm.provider`, so `agent_key` does not currently
    change which provider is returned. Use `model_for(agent_key)`
    separately to resolve which model ID to pass to `complete()`.
    """
    name = load_llm_config().get("provider") or DEFAULT_PROVIDER
    return _build_provider(name)
