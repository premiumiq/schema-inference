"""Provider-neutral LLM types.

Content blocks use the same shape Anthropic's wire format already uses
(`{"type": "text", "text": ...}`, `{"type": "tool_use", "id", "name",
"input"}`, `{"type": "tool_result", "tool_use_id", "content"}`) rather than
inventing a new IR — that shape is already a reasonable lingua franca for a
system-prompt + tool-use conversation, the same reasoning the plan applies
to `LLMToolDef` mirroring `tools.py`'s `TOOL_SCHEMAS` shape. Each provider
adapter (see `providers/`) is responsible for translating this neutral shape
into its own wire format at the call boundary — Anthropic's adapter is
close to a pass-through, OpenAI's adapter maps `tool_use`/`tool_result`
blocks onto `tool_calls`/`role: "tool"` messages.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class LLMMessage(BaseModel):
    """One turn in a conversation.

    `content` is either plain text or a list of content-block dicts (each
    with a `type` of `text` | `tool_use` | `tool_result`). Using plain dicts
    here (rather than a strict block union) means a provider response's
    `content` — itself a list of such dicts, see `LLMResponse` — can be
    appended straight back into the next request's `messages` list without
    any conversion, exactly how `mapping_agent.py`'s tool-use loop already
    re-appends `response.content` today.
    """

    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class LLMToolDef(BaseModel):
    """A tool definition in JSON-Schema form.

    Structurally identical to `tools.py`'s `TOOL_SCHEMAS` entries (`name`,
    `description`, `input_schema`) — that shape is already provider-neutral
    JSON Schema, so `tools.py` itself needs no changes. Each provider
    adapter translates this into its own wire shape: Anthropic's
    `input_schema` key as-is, OpenAI's `parameters` key nested under a
    `function` wrapper.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    cache_control: dict[str, Any] | None = None
    # Anthropic-specific prompt-caching hint (see mapping_agent.py). The
    # Anthropic adapter applies it to the wire request; adapters for
    # backends with no equivalent (OpenAI-compatible) silently strip it
    # rather than erroring — those backends have no such concept.


class LLMToolCall(BaseModel):
    """A single tool-use request from the model, normalized across providers."""

    id: str
    name: str
    input: dict[str, Any]


class LLMResponse(BaseModel):
    """Normalized model response.

    `content` is a list of content-block dicts in the same shape
    `LLMMessage.content` uses, so it can be appended directly to the next
    request's `messages` list to continue a multi-turn tool-use
    conversation.
    """

    content: list[dict[str, Any]]
    stop_reason: str | None = None
    model: str | None = None

    @property
    def text(self) -> str:
        """Concatenated text from every text-type content block."""
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_calls(self) -> list[LLMToolCall]:
        """Every tool_use content block, normalized to LLMToolCall."""
        return [
            LLMToolCall(id=b["id"], name=b["name"], input=b.get("input", {}))
            for b in self.content
            if b.get("type") == "tool_use"
        ]
