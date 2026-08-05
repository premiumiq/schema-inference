"""AnthropicProvider — wraps anthropic.Anthropic().

Must produce byte-for-byte the same requests/behavior the pipeline had
before MAP-8: the same model IDs (supplied by the caller via `model=`,
resolved through registry.model_for()), the same system/messages/tools
shapes, the same `cache_control` prompt-caching markers, and the same
rate-limit signal to throttle.py's retry loop (now normalized to
LLMRateLimitError instead of a bare `anthropic.RateLimitError`).

The `anthropic` package is imported lazily, inside the methods that need
it — matching mapper.py's existing guarded-import pattern for the LLM pass
(`_run_llm_batch`) — so `import schema_inference.llm` / `get_provider()`
never requires the anthropic package to be installed unless the Anthropic
provider is actually used.
"""

from __future__ import annotations

import os
from typing import Any

from ..errors import LLMAPIError, LLMAuthError, LLMRateLimitError
from ..provider import LLMProvider
from ..types import LLMMessage, LLMResponse, LLMToolDef


class AnthropicProvider(LLMProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._client = None  # constructed lazily on first complete() call

    def _client_instance(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "anthropic package required for the 'anthropic' LLM provider. "
                "Install: pip install anthropic"
            ) from e

        api_key_env = self._config.get("api_key_env") or "ANTHROPIC_API_KEY"
        api_key = os.environ.get(api_key_env)
        # anthropic.Anthropic() with no api_key reads ANTHROPIC_API_KEY
        # itself; only pass an explicit key when api_key_env names a
        # different variable than the SDK's own default, so behavior is
        # identical to today's bare anthropic.Anthropic() in the common case.
        if api_key_env != "ANTHROPIC_API_KEY":
            if not api_key:
                # A custom api_key_env was explicitly configured but isn't
                # populated -- fail loudly. Falling through to bare
                # anthropic.Anthropic() here would read ANTHROPIC_API_KEY
                # instead, masking a config typo (api_key_env set to a var
                # name that was never exported) as an unrelated downstream
                # auth failure, or worse, a silent switch to whatever
                # credential ANTHROPIC_API_KEY happens to hold.
                raise LLMAuthError(
                    f"llm.providers.anthropic.api_key_env is set to "
                    f"'{api_key_env}', but that environment variable is "
                    f"unset or empty. Export {api_key_env}, or remove the "
                    f"api_key_env override to use the default ANTHROPIC_API_KEY."
                )
            self._client = anthropic.Anthropic(api_key=api_key)
        else:
            self._client = anthropic.Anthropic()
        return self._client

    def complete(
        self,
        system: str | list[dict[str, Any]],
        messages: list[LLMMessage | dict[str, Any]],
        tools: list[LLMToolDef] | None = None,
        max_tokens: int = 4096,
        model: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        import anthropic

        client = self._client_instance()

        raw_messages = [
            {"role": m.role, "content": m.content} if isinstance(m, LLMMessage) else m
            for m in messages
        ]

        request: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=raw_messages,
        )
        if tools:
            request["tools"] = [self._tool_wire_shape(t) for t in tools]
        request.update(kwargs)

        try:
            response = client.messages.create(**request)
        except anthropic.RateLimitError as e:
            raise LLMRateLimitError(str(e)) from e
        except anthropic.AuthenticationError as e:
            raise LLMAuthError(str(e)) from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise LLMAPIError(str(e)) from e

        content = [self._block_to_dict(b) for b in response.content]
        return LLMResponse(
            content=content,
            stop_reason=response.stop_reason,
            model=response.model,
        )

    @staticmethod
    def _tool_wire_shape(t: LLMToolDef) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        if t.cache_control:
            wire["cache_control"] = t.cache_control
        return wire

    @staticmethod
    def _block_to_dict(block: Any) -> dict[str, Any]:
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
        # Any other block type (e.g. thinking) — pass through as a plain
        # dict so nothing is silently dropped; callers that don't care
        # simply won't match on its `type`.
        return block.model_dump()
