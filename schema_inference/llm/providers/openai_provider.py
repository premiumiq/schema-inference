"""OpenAIProvider — wraps the `openai` package's Chat Completions API.

Covers OpenAI directly, and — via `base_url` in the provider's
agent_config.yml block — any OpenAI-compatible server: Azure OpenAI,
Ollama, vLLM, LM Studio, etc. One adapter buys most of the "bring your own
model" ask (MAP-8's stated goal) rather than hand-rolling one adapter per
backend.

The `openai` package is imported lazily, inside the methods that need it —
matching the `anthropic` package's guarded-import pattern elsewhere in this
repo (see mapper.py's `_run_llm_batch`, AnthropicProvider) — so
`get_provider()` never requires `openai` to be installed unless the
`openai` provider is actually selected in agent_config.yml.

Translation notes (Anthropic-shaped neutral IR -> OpenAI wire format):
  - `system` becomes a leading `{"role": "system", "content": ...}` message
    (a content-block list is flattened to its text, since OpenAI has no
    prompt-caching `cache_control` concept).
  - `LLMToolDef.cache_control` is silently dropped — OpenAI-compatible
    backends have no equivalent, so the adapter strips it rather than
    erroring, per MAP-8's requirement.
  - An assistant `tool_use` content block becomes an OpenAI `tool_calls`
    entry (`function.arguments` JSON-encoded from `input`).
  - A user `tool_result` content block becomes its own OpenAI message with
    `role: "tool"` — OpenAI represents each tool result as a separate
    message, not a content block nested inside a user message the way
    Anthropic does, so one neutral message with N tool_result blocks
    expands to N `role: "tool"` messages.
"""

from __future__ import annotations

import json
from typing import Any

from ..errors import LLMAPIError, LLMAuthError, LLMRateLimitError
from ..provider import LLMProvider
from ..types import LLMMessage, LLMResponse, LLMToolDef


class OpenAIProvider(LLMProvider):
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._client = None  # constructed lazily on first complete() call

    def _client_instance(self):
        if self._client is not None:
            return self._client
        try:
            import openai
        except ImportError as e:
            raise ImportError(
                "openai package required for the 'openai' LLM provider. "
                "Install: pip install openai"
            ) from e

        import os

        api_key_env = self._config.get("api_key_env") or "OPENAI_API_KEY"
        api_key = os.environ.get(api_key_env)
        base_url = self._config.get("base_url") or None
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**kwargs)
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
        import openai

        client = self._client_instance()

        oai_messages: list[dict[str, Any]] = []
        system_text = self._flatten_system(system)
        if system_text:
            oai_messages.append({"role": "system", "content": system_text})

        for m in messages:
            role = m.role if isinstance(m, LLMMessage) else m["role"]
            content = m.content if isinstance(m, LLMMessage) else m["content"]
            oai_messages.extend(self._translate_message(role, content))

        request: dict[str, Any] = dict(
            model=model,
            max_tokens=max_tokens,
            messages=oai_messages,
        )
        if tools:
            request["tools"] = [self._tool_wire_shape(t) for t in tools]
        request.update(kwargs)

        try:
            response = client.chat.completions.create(**request)
        except openai.RateLimitError as e:
            raise LLMRateLimitError(str(e)) from e
        except openai.AuthenticationError as e:
            raise LLMAuthError(str(e)) from e
        except (openai.APIStatusError, openai.APIConnectionError) as e:
            raise LLMAPIError(str(e)) from e

        choice = response.choices[0]
        content = self._response_to_content(choice.message)
        return LLMResponse(
            content=content,
            stop_reason=choice.finish_reason,
            model=response.model,
        )

    # ── Translation helpers ─────────────────────────────────────────────

    @staticmethod
    def _flatten_system(system: str | list[dict[str, Any]]) -> str:
        if isinstance(system, str):
            return system
        return "\n\n".join(block.get("text", "") for block in system if block.get("type") == "text")

    @staticmethod
    def _tool_wire_shape(t: LLMToolDef) -> dict[str, Any]:
        # cache_control is Anthropic-only prompt-caching metadata; OpenAI-
        # compatible backends have no equivalent, so it is intentionally
        # dropped here rather than forwarded or erroring.
        return {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }

    @classmethod
    def _translate_message(cls, role: str, content: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"role": role, "content": content}]

        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_messages: list[dict[str, Any]] = []

        for block in content:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            elif btype == "tool_result":
                # OpenAI has no "user" message with embedded tool results —
                # each tool result is its own role:"tool" message.
                tool_messages.append({
                    "role": "tool",
                    "tool_call_id": block["tool_use_id"],
                    "content": block.get("content", ""),
                })

        if tool_messages:
            # A neutral "user" turn carrying only tool_result blocks maps
            # to tool messages only — nothing else to emit for that turn.
            return tool_messages

        msg: dict[str, Any] = {"role": role, "content": "".join(text_parts) or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        return [msg]

    @staticmethod
    def _response_to_content(message: Any) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in getattr(message, "tool_calls", None) or []:
            try:
                parsed_input = json.loads(call.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                parsed_input = {}
            blocks.append({
                "type": "tool_use",
                "id": call.id,
                "name": call.function.name,
                "input": parsed_input,
            })
        return blocks
