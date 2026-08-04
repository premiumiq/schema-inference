"""ABC for LLM provider adapters.

Implementations live in providers/ — one module per backend. Every call
site in the mapping pipeline (mapper.py, agents/mapping_agent.py,
agents/critic_agent.py, agents/sql_agent.py, tools/tune_prompts.py) talks to
this interface via registry.get_provider(), never to a provider SDK
directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .types import LLMMessage, LLMResponse, LLMToolDef


class LLMProvider(ABC):
    """A thin adapter over one LLM backend's wire format."""

    @abstractmethod
    def complete(
        self,
        system: str | list[dict[str, Any]],
        messages: list[LLMMessage | dict[str, Any]],
        tools: list[LLMToolDef] | None = None,
        max_tokens: int = 4096,
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Send one request to the backend and return a normalized response.

        Args:
            system:     Plain string, or a list of content blocks (the
                        latter is how Anthropic's prompt-caching
                        `cache_control` marker is attached to the system
                        prompt — see mapping_agent.py). Adapters for
                        backends with no equivalent concept collapse a
                        block list back to plain text.
            messages:   Conversation turns so far, oldest first.
            tools:      Tool definitions available this turn, or None.
            max_tokens: Maximum tokens to generate.
            model:      Backend-specific model ID. Every current call site
                        always supplies this explicitly (resolved via
                        registry.model_for()) rather than relying on an
                        adapter default, to keep model selection visible
                        and config-driven at the call site.
            **kwargs:   Passed through to the backend request unchanged
                        (e.g. Anthropic's `tool_choice`).

        Returns:
            LLMResponse with normalized `content` blocks.

        Raises:
            schema_inference.llm.errors.LLMRateLimitError,
            LLMAuthError, LLMAPIError — normalized across backends.
        """
        raise NotImplementedError
