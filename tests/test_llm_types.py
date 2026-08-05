from schema_inference.agents.tools import TOOL_SCHEMAS
from schema_inference.llm.types import LLMResponse, LLMToolCall, LLMToolDef


def test_llm_response_text_concatenates_text_blocks_only():
    response = LLMResponse(content=[
        {"type": "text", "text": "hello "},
        {"type": "tool_use", "id": "t1", "name": "foo", "input": {}},
        {"type": "text", "text": "world"},
    ])
    assert response.text == "hello world"


def test_llm_response_text_is_empty_string_when_no_text_blocks():
    response = LLMResponse(content=[{"type": "tool_use", "id": "t1", "name": "foo", "input": {}}])
    assert response.text == ""


def test_llm_response_tool_calls_extracts_tool_use_blocks_only():
    response = LLMResponse(content=[
        {"type": "text", "text": "thinking..."},
        {"type": "tool_use", "id": "t1", "name": "lookup_canonical", "input": {"query": "POL_NO"}},
    ])
    assert response.tool_calls == [
        LLMToolCall(id="t1", name="lookup_canonical", input={"query": "POL_NO"})
    ]


def test_llm_response_tool_calls_is_empty_list_when_no_tool_use_blocks():
    response = LLMResponse(content=[{"type": "text", "text": "just text"}])
    assert response.tool_calls == []


def test_llm_tooldef_mirrors_tools_py_schemas_with_no_conversion():
    """MAP-8's design premise: TOOL_SCHEMAS entries (name, description,
    input_schema) are already provider-neutral JSON Schema, so LLMToolDef
    should accept them as-is -- tools.py itself needs no changes."""
    for schema in TOOL_SCHEMAS:
        tool_def = LLMToolDef(**schema)
        assert tool_def.name == schema["name"]
        assert tool_def.description == schema["description"]
        assert tool_def.input_schema == schema["input_schema"]
        assert tool_def.cache_control is None


def test_llm_tooldef_cache_control_is_optional_and_anthropic_specific():
    tool_def = LLMToolDef(
        name="x", description="d", input_schema={"type": "object"},
        cache_control={"type": "ephemeral"},
    )
    assert tool_def.cache_control == {"type": "ephemeral"}
