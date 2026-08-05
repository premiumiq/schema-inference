import anthropic
import httpx
import pytest

import schema_inference.llm.registry as registry
from schema_inference.llm.errors import LLMAPIError, LLMAuthError, LLMRateLimitError
from schema_inference.llm.providers.anthropic_provider import AnthropicProvider
from schema_inference.llm.types import LLMMessage, LLMToolCall, LLMToolDef


# ── Fakes standing in for the anthropic SDK's client/response shapes ───────

class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class _FakeMessage:
    def __init__(self, content, stop_reason="end_turn", model="claude-haiku-4-5-20251001"):
        self.content = content
        self.stop_reason = stop_reason
        self.model = model


class _FakeMessagesEndpoint:
    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._exception is not None:
            raise self._exception
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response=None, exception=None):
        self.messages = _FakeMessagesEndpoint(response=response, exception=exception)


def _provider_with_fake_client(response=None, exception=None):
    provider = AnthropicProvider()
    fake = _FakeAnthropicClient(response=response, exception=exception)
    provider._client = fake  # bypass real client construction -- no API key needed
    return provider, fake


def _http_exception(exc_cls, status):
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    if exc_cls is anthropic.APIConnectionError:
        return exc_cls(request=req)
    resp = httpx.Response(status, request=req)
    return exc_cls("boom", response=resp, body=None)


# ── complete(): request shape + response translation ───────────────────────

def test_complete_sends_a_byte_for_byte_equivalent_request():
    """No tools, plain-string system -- must match exactly what
    client.messages.create(...) received before MAP-8."""
    provider, fake = _provider_with_fake_client(response=_FakeMessage([_FakeTextBlock("hello world")]))

    result = provider.complete(
        system="be nice", messages=[{"role": "user", "content": "hi"}],
        max_tokens=64, model="claude-haiku-4-5-20251001",
    )

    assert fake.messages.last_kwargs == {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 64,
        "system": "be nice",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert result.text == "hello world"
    assert result.content == [{"type": "text", "text": "hello world"}]
    assert result.stop_reason == "end_turn"
    assert result.model == "claude-haiku-4-5-20251001"


def test_complete_translates_llmmessage_instances_to_wire_dicts():
    provider, fake = _provider_with_fake_client(response=_FakeMessage([_FakeTextBlock("ok")]))
    provider.complete(
        system="s", messages=[LLMMessage(role="user", content="hi")],
        max_tokens=10, model="m",
    )
    assert fake.messages.last_kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_complete_translates_tool_use_response_into_llm_tool_calls():
    provider, _ = _provider_with_fake_client(
        response=_FakeMessage([_FakeToolUseBlock("toolu_1", "lookup_canonical", {"query": "POL_NO"})])
    )
    result = provider.complete(
        system="s", messages=[{"role": "user", "content": "map POL_NO"}],
        max_tokens=64, model="m",
        tools=[LLMToolDef(name="lookup_canonical", description="d", input_schema={"type": "object"})],
    )
    assert result.tool_calls == [LLMToolCall(id="toolu_1", name="lookup_canonical", input={"query": "POL_NO"})]
    assert result.text == ""


def test_complete_puts_tools_in_anthropic_input_schema_wire_shape():
    provider, fake = _provider_with_fake_client(response=_FakeMessage([_FakeTextBlock("ok")]))
    tool = LLMToolDef(
        name="lookup_canonical", description="d", input_schema={"type": "object"},
        cache_control={"type": "ephemeral"},
    )
    provider.complete(
        system="s", messages=[{"role": "user", "content": "hi"}],
        max_tokens=64, model="m", tools=[tool],
    )
    assert fake.messages.last_kwargs["tools"] == [{
        "name": "lookup_canonical",
        "description": "d",
        "input_schema": {"type": "object"},
        "cache_control": {"type": "ephemeral"},
    }]


def test_complete_omits_tools_key_when_no_tools_supplied():
    provider, fake = _provider_with_fake_client(response=_FakeMessage([_FakeTextBlock("ok")]))
    provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=10, model="m")
    assert "tools" not in fake.messages.last_kwargs


# ── complete(): exception normalization ─────────────────────────────────────

@pytest.mark.parametrize("native_exc_cls, status, normalized", [
    (anthropic.RateLimitError, 429, LLMRateLimitError),
    (anthropic.AuthenticationError, 401, LLMAuthError),
    (anthropic.APIStatusError, 500, LLMAPIError),
    (anthropic.APIConnectionError, None, LLMAPIError),
])
def test_complete_normalizes_anthropic_exceptions(native_exc_cls, status, normalized):
    exc = _http_exception(native_exc_cls, status)
    provider, _ = _provider_with_fake_client(exception=exc)
    with pytest.raises(normalized):
        provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=10, model="m")


# ── _client_instance(): api_key_env resolution ──────────────────────────────

def test_client_instance_raises_loudly_on_unset_custom_api_key_env(monkeypatch):
    """A configured api_key_env that isn't actually populated must fail
    loudly, not silently fall back to reading ANTHROPIC_API_KEY instead."""
    monkeypatch.delenv("MY_CUSTOM_KEY", raising=False)
    provider = AnthropicProvider(config={"api_key_env": "MY_CUSTOM_KEY"})
    with pytest.raises(LLMAuthError, match="MY_CUSTOM_KEY"):
        provider._client_instance()


def test_client_instance_uses_custom_api_key_env_when_populated(monkeypatch):
    monkeypatch.setenv("MY_CUSTOM_KEY", "sk-test-123")
    provider = AnthropicProvider(config={"api_key_env": "MY_CUSTOM_KEY"})
    client = provider._client_instance()
    assert client.api_key == "sk-test-123"


# ── get_provider() -> AnthropicProvider -> complete() end to end ───────────

def test_get_provider_returns_anthropic_provider_and_complete_translates_response(monkeypatch, tmp_path):
    """MAP-8 verification: get_provider() (no API key needed, config
    missing) resolves to AnthropicProvider, and .complete() against a
    mocked anthropic client produces a correct LLMResponse."""
    registry._build_provider.cache_clear()
    monkeypatch.setattr(registry, "_AGENT_CONFIG_PATH", tmp_path / "missing.yml")
    try:
        provider = registry.get_provider()
        assert isinstance(provider, AnthropicProvider)

        provider._client = _FakeAnthropicClient(
            response=_FakeMessage([_FakeTextBlock("mapped to policy_id")])
        )
        result = provider.complete(
            system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=10, model="m"
        )
        assert result.text == "mapped to policy_id"
    finally:
        registry._build_provider.cache_clear()
