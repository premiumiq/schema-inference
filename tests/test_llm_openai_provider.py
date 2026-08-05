"""OpenAIProvider tests.

The translation helpers (tool-schema shape, message shape, response shape)
are pure functions operating on plain dicts/objects and are tested
unconditionally -- they don't import the `openai` package (it's imported
lazily, only inside complete()/_client_instance(), matching the guarded-
import pattern used for `anthropic` elsewhere in this repo). The
complete()-level tests do need `openai` installed (an optional extra, see
pyproject.toml's `openai` extra) and skip cleanly via importorskip when
it isn't -- e.g. in CI, which only installs the base + dev dependencies.
"""

from types import SimpleNamespace

import pytest

from schema_inference.llm.providers.openai_provider import OpenAIProvider
from schema_inference.llm.types import LLMToolDef


# ── Tool-schema translation ──────────────────────────────────────────────

def test_tool_wire_shape_wraps_function_and_drops_cache_control():
    """OpenAI has no prompt-caching concept -- cache_control must be
    silently dropped, not forwarded and not an error (MAP-8 requirement)."""
    tool = LLMToolDef(
        name="lookup_canonical", description="find a field",
        input_schema={"type": "object", "properties": {}},
        cache_control={"type": "ephemeral"},
    )
    wire = OpenAIProvider._tool_wire_shape(tool)
    assert wire == {
        "type": "function",
        "function": {
            "name": "lookup_canonical",
            "description": "find a field",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_tool_wire_shape_without_cache_control():
    tool = LLMToolDef(name="x", description="d", input_schema={"type": "object"})
    wire = OpenAIProvider._tool_wire_shape(tool)
    assert wire["function"]["parameters"] == {"type": "object"}
    assert "cache_control" not in wire and "cache_control" not in wire["function"]


# ── system prompt flattening ─────────────────────────────────────────────

def test_flatten_system_passes_through_plain_string():
    assert OpenAIProvider._flatten_system("be helpful") == "be helpful"


def test_flatten_system_flattens_anthropic_style_block_list_and_drops_cache_control():
    system = [
        {"type": "text", "text": "part one", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "part two"},
    ]
    assert OpenAIProvider._flatten_system(system) == "part one\n\npart two"


# ── message translation ──────────────────────────────────────────────────

def test_translate_message_plain_text():
    assert OpenAIProvider._translate_message("user", "hello") == [{"role": "user", "content": "hello"}]


def test_translate_message_assistant_tool_use_becomes_tool_calls():
    content = [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "call_1", "name": "lookup_canonical", "input": {"query": "POL_NO"}},
    ]
    out = OpenAIProvider._translate_message("assistant", content)
    assert out == [{
        "role": "assistant",
        "content": "let me check",
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "lookup_canonical", "arguments": '{"query": "POL_NO"}'},
        }],
    }]


def test_translate_message_tool_result_becomes_its_own_tool_role_messages():
    """Anthropic nests N tool_result blocks inside one user message; OpenAI
    represents each result as its own role:'tool' message -- one neutral
    message with two tool_result blocks must expand to two OpenAI messages."""
    content = [
        {"type": "tool_result", "tool_use_id": "call_1", "content": '{"ok": true}'},
        {"type": "tool_result", "tool_use_id": "call_2", "content": '{"ok": false}'},
    ]
    out = OpenAIProvider._translate_message("user", content)
    assert out == [
        {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true}'},
        {"role": "tool", "tool_call_id": "call_2", "content": '{"ok": false}'},
    ]


def test_translate_message_text_only_content_list():
    content = [{"type": "text", "text": "hi"}]
    assert OpenAIProvider._translate_message("user", content) == [{"role": "user", "content": "hi"}]


# ── response translation ─────────────────────────────────────────────────

def test_response_to_content_translates_text_and_tool_calls():
    message = SimpleNamespace(
        content="here you go",
        tool_calls=[
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="lookup_canonical", arguments='{"query": "POL_NO"}'),
            ),
        ],
    )
    blocks = OpenAIProvider._response_to_content(message)
    assert blocks == [
        {"type": "text", "text": "here you go"},
        {"type": "tool_use", "id": "call_1", "name": "lookup_canonical", "input": {"query": "POL_NO"}},
    ]


def test_response_to_content_handles_malformed_tool_arguments_gracefully():
    message = SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(id="call_1", function=SimpleNamespace(name="foo", arguments="not json"))],
    )
    blocks = OpenAIProvider._response_to_content(message)
    assert blocks == [{"type": "tool_use", "id": "call_1", "name": "foo", "input": {}}]


def test_response_to_content_no_text_no_tool_calls_is_empty():
    message = SimpleNamespace(content=None, tool_calls=None)
    assert OpenAIProvider._response_to_content(message) == []


# ── complete(): full request/response round trip (needs `openai` installed) ─

class _FakeCompletions:
    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._exception is not None:
            raise self._exception
        return self._response


class _FakeOpenAIClient:
    def __init__(self, response=None, exception=None):
        self.chat = SimpleNamespace(completions=_FakeCompletions(response=response, exception=exception))


def _provider_with_fake_client(response=None, exception=None):
    provider = OpenAIProvider()
    fake = _FakeOpenAIClient(response=response, exception=exception)
    provider._client = fake  # bypass real client construction -- no API key needed
    return provider, fake


def test_complete_end_to_end_text_response():
    pytest.importorskip("openai")
    message = SimpleNamespace(content="hi there", tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], model="gpt-4o-mini")
    provider, fake = _provider_with_fake_client(response=response)

    result = provider.complete(
        system="be nice", messages=[{"role": "user", "content": "hi"}],
        max_tokens=64, model="gpt-4o-mini",
    )

    assert result.text == "hi there"
    assert result.stop_reason == "stop"
    assert result.model == "gpt-4o-mini"
    assert fake.chat.completions.last_kwargs["messages"][0] == {"role": "system", "content": "be nice"}
    assert fake.chat.completions.last_kwargs["messages"][1] == {"role": "user", "content": "hi"}


def test_complete_drops_cache_control_and_uses_function_tool_shape_end_to_end():
    pytest.importorskip("openai")
    message = SimpleNamespace(content="ok", tool_calls=None)
    response = SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], model="gpt-4o-mini")
    provider, fake = _provider_with_fake_client(response=response)

    tool = LLMToolDef(
        name="lookup_canonical", description="d", input_schema={"type": "object"},
        cache_control={"type": "ephemeral"},
    )
    provider.complete(
        system="s", messages=[{"role": "user", "content": "hi"}],
        max_tokens=10, model="gpt-4o-mini", tools=[tool],
    )
    sent_tools = fake.chat.completions.last_kwargs["tools"]
    assert sent_tools == [{
        "type": "function",
        "function": {"name": "lookup_canonical", "description": "d", "parameters": {"type": "object"}},
    }]


def _http_exception(exc_cls, status):
    import httpx
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    if status is None:
        return exc_cls(request=req)
    resp = httpx.Response(status, request=req)
    return exc_cls("boom", response=resp, body=None)


def test_complete_normalizes_rate_limit_error():
    openai = pytest.importorskip("openai")
    from schema_inference.llm.errors import LLMRateLimitError
    exc = _http_exception(openai.RateLimitError, 429)
    provider, _ = _provider_with_fake_client(exception=exc)
    with pytest.raises(LLMRateLimitError):
        provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=10, model="m")


def test_complete_normalizes_authentication_error():
    openai = pytest.importorskip("openai")
    from schema_inference.llm.errors import LLMAuthError
    exc = _http_exception(openai.AuthenticationError, 401)
    provider, _ = _provider_with_fake_client(exception=exc)
    with pytest.raises(LLMAuthError):
        provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=10, model="m")


def test_complete_normalizes_status_and_connection_errors():
    openai = pytest.importorskip("openai")
    from schema_inference.llm.errors import LLMAPIError
    status_exc = _http_exception(openai.APIStatusError, 500)
    provider, _ = _provider_with_fake_client(exception=status_exc)
    with pytest.raises(LLMAPIError):
        provider.complete(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=10, model="m")

    conn_exc = _http_exception(openai.APIConnectionError, None)
    provider2, _ = _provider_with_fake_client(exception=conn_exc)
    with pytest.raises(LLMAPIError):
        provider2.complete(system="s", messages=[{"role": "user", "content": "hi"}], max_tokens=10, model="m")
