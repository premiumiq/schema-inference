"""MappingAgent — per-column tool-use loop that replaces _run_llm_batch.

For each low-confidence column, we open a conversation with Claude Haiku and let
it call tools (lookup_canonical, check_value_catalog, etc.) to investigate before
deciding on a mapping. This replaces the old single-shot batch call, which guessed
from the column name alone with no ability to look anything up.

Flow per column:
  1. Send Claude the column profile + instructions + tool definitions.
  2. Claude calls a tool -> we execute it -> we return the result.
  3. Repeat up to max_tool_calls_per_column (agent_config.yml, falls back to
     MAX_TOOL_CALLS), then force a final answer.
  4. Parse Claude's final answer into a ColumnMapping + an AgentTrace.

Columns are processed concurrently via asyncio (default 10 parallel).
"""

from __future__ import annotations

import asyncio
import json

from ..metamodel.few_shot import format_examples_block, retrieve_examples
from ..models import AgentToolCall, AgentTrace, ColumnMapping, ColumnProfile
from .throttle import acall_with_retry
from .tools import TOOL_DISPATCH, TOOL_SCHEMAS, register_profiles

MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_CALLS = 5
DEFAULT_CONCURRENCY = 10

# Prompt caching: system prompt + tool schemas are identical across every
# column and every call within one run (system_prompt is resolved once per
# run_mapping_agent() call, TOOL_SCHEMAS never changes) — exactly the repeated
# prefix Anthropic's cache_control is for. No-op (silently ignored, not an
# error) if the prefix is below the model's minimum cacheable length; safe to
# leave on unconditionally. Cuts input-token cost on every call after the first
# within the 5-min cache window — biggest single lever for MappingAgent cost.
_CACHE_CONTROL = {"type": "ephemeral"}
_TOOL_SCHEMAS_CACHED = [dict(t) for t in TOOL_SCHEMAS]
_TOOL_SCHEMAS_CACHED[-1] = {**_TOOL_SCHEMAS_CACHED[-1], "cache_control": _CACHE_CONTROL}


def _max_tool_calls() -> int:
    """mapping_agent.max_tool_calls_per_column from agent_config.yml, falling
    back to the hardcoded MAX_TOOL_CALLS. Was previously decorative — config
    declared this knob but the code ignored it; now wired the same way as
    _rule_weights()/_active_system_prompt()."""
    try:
        from .orchestrator import load_agent_config
        return int(load_agent_config().get("mapping_agent", {}).get("max_tool_calls_per_column", MAX_TOOL_CALLS))
    except Exception:
        return MAX_TOOL_CALLS

_SYSTEM_PROMPT = """You are an insurance data engineering agent. Your job is to map ONE \
source column from a legacy policy admin system (PAS-L) to a canonical insurance policy \
schema, OR decide it belongs in extended_attributes (no canonical mapping).

You have tools. USE THEM before deciding. A column name alone is not enough:
- lookup_canonical: find candidate target fields by name
- check_value_catalog: discover the column's true type, value map, and defects.
  Column names can be misleading about actual encoding — always verify before deciding
  (e.g. an amount column may be integer cents rather than decimal dollars; a column whose
  name resembles an ID may actually hold a string code, not a numeric identifier).
- score_name_similarity: compare a source column to a candidate target
- get_column_profile: see sample values, null rate, inferred type, flags
- get_hard_columns: check whether this column is a known hard case

IMPORTANT REASONING RULES:
- A strong name match is NOT sufficient. Always check the value catalog for hard columns.
- If the value catalog note says a column is NOT a given field, trust it and route to
  extended_attributes (target_field = null).
- A column name resembling a canonical field is not proof it IS that field — verify its
  actual semantics (granularity, time period, whose attribute it is) actually match before
  committing to that target.

When you have gathered enough information, respond with your FINAL ANSWER as a JSON object
(and nothing else) in this exact form:
{
  "target_field": "<canonical field name, or null for extended_attributes>",
  "confidence": <float 0.0-1.0>,
  "reasoning": "<one or two sentences explaining the decision>"
}"""


def _active_system_prompt() -> str:
    """MAP-4 Layer 2: the metamodel's most recently human-accepted prompt for
    'mapping', if any; else the hardcoded _SYSTEM_PROMPT above. Mirrors
    mapper.py's _rule_weights() pattern (Layer 0) — config/metamodel-driven
    override with a code-level fallback, so accepting a tuned prompt via
    tools/tune_prompts.py --accept takes effect on production runs with zero
    other code changes."""
    try:
        from ..metamodel.store import open_store
    except ImportError:
        return _SYSTEM_PROMPT
    store = open_store()
    if not store:
        return _SYSTEM_PROMPT
    try:
        return store.get_active_prompt("mapping") or _SYSTEM_PROMPT
    finally:
        store.close()



def _build_user_prompt(col: ColumnProfile, source_name: str) -> str:
    """The initial message describing the column to map.

    MAP-4 Layer 1: prepends a few-shot block of similar past examples
    (retrieved from the metamodel store) when any clear the similarity
    threshold. Empty string from retrieve_examples()/format_examples_block()
    when the bank is empty or unavailable — no branch needed here.
    """
    examples_block = format_examples_block(retrieve_examples(source_name, col))
    examples_section = f"{examples_block}\n\n" if examples_block else ""

    return (
        f"{examples_section}"
        f"Map this source column.\n\n"
        f"Column name: {col.name}\n"
        f"Inferred type: {col.inferred_type}\n"
        f"Null rate: {col.null_rate:.2f}\n"
        f"Distinct count: {col.distinct_count}\n"
        f"Sample values: {col.sample_values[:5]}\n"
        f"is_id_column: {col.is_id_column}, "
        f"is_coded_column: {col.is_coded_column}, "
        f"is_cents_integer: {col.is_cents_integer}, "
        f"date_format: {col.date_format}\n\n"
        f"Investigate using your tools, then give your final JSON answer."
    )


def _extract_final_answer(text: str) -> dict:
    """Pull the final JSON answer out of Claude's last text block.

    Handles answers that include analysis prose before a ```json fenced block,
    bare JSON, or JSON with surrounding text. Strategy: prefer a fenced json
    block; otherwise fall back to the last {...} object in the text.
    """
    import re as _re

    # 1. Prefer an explicit ```json ... ``` fenced block (take the LAST one)
    fenced = _re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, _re.DOTALL)
    if fenced:
        return json.loads(fenced[-1])

    # 2. Otherwise, grab the last balanced-looking {...} object in the text
    #    (last, because the final answer comes after any prose/analysis).
    candidates = _re.findall(r"\{[^{}]*\"target_field\"[^{}]*\}", text, _re.DOTALL)
    if candidates:
        return json.loads(candidates[-1])

    # 3. Last resort: first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1:
        return json.loads(text[start : end + 1])

    raise ValueError("No JSON object found in answer")


async def _map_one_column(
    client,
    col: ColumnProfile,
    source_table: str,
    source_name: str,
    system_prompt: str,
    max_tool_calls: int,
) -> tuple[ColumnMapping, AgentTrace]:
    """Run the tool-use loop for a single column. Returns (mapping, trace)."""

    messages = [{"role": "user", "content": _build_user_prompt(col, source_name)}]
    tool_calls_log: list[AgentToolCall] = []

    final: dict | None = None
    cached_system = [{"type": "text", "text": system_prompt, "cache_control": _CACHE_CONTROL}]

    for _ in range(max_tool_calls + 1):
        # On the last allowed turn, drop tools to force a text answer
        use_tools = len(tool_calls_log) < max_tool_calls

        kwargs = dict(
            model=MODEL,
            max_tokens=1024,
            system=cached_system,
            messages=messages,
        )
        if use_tools:
            kwargs["tools"] = _TOOL_SCHEMAS_CACHED

        # The anthropic SDK call is synchronous; run it in a thread so asyncio
        # can run other columns concurrently. Retry on rate-limit (429) with backoff.
        response = await acall_with_retry(client, kwargs)

        # Did Claude ask to use a tool?
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if tool_use_blocks and use_tools:
            # Append Claude's turn (its tool requests) to the conversation
            messages.append({"role": "assistant", "content": response.content})

            # Execute every requested tool and collect the results
            tool_results = []
            for block in tool_use_blocks:
                fn = TOOL_DISPATCH.get(block.name)
                try:
                    result = fn(**block.input) if fn else None
                except Exception as e:  # noqa: BLE001
                    result = {"error": str(e)}
                result_json = json.dumps(result, default=str)

                tool_calls_log.append(
                    AgentToolCall(
                        tool_name=block.name,
                        inputs=dict(block.input),
                        output=result_json,
                    )
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_json,
                    }
                )

            # Send the tool results back so Claude can continue reasoning
            messages.append({"role": "user", "content": tool_results})
            continue

        # No tool use -> Claude gave a text answer. Parse it and stop.
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            final = _extract_final_answer(text)
        except Exception:  # noqa: BLE001
            final = {"target_field": None, "confidence": 0.30,
                     "reasoning": "Could not parse agent answer; routed to extended_attributes."}
        break

    if final is None:
        final = {"target_field": None, "confidence": 0.30,
                 "reasoning": "Agent did not produce a final answer within tool budget."}

    # Normalize target ("null"/"" -> None)
    target = final.get("target_field")
    if target in ("", "null", "None", "extended_attributes"):
        target = None
    confidence = float(final.get("confidence", 0.40))
    reasoning = final.get("reasoning", "")

    # Generate SQL for the chosen mapping (passthrough if no target)
    if target:
        sql = TOOL_DISPATCH["generate_sql"](col.name, target)
    else:
        sql = col.name

    mapping = ColumnMapping(
        source_column=col.name,
        source_table=source_table,
        target_field=target,
        confidence=round(confidence, 4),
        method="llm",
        sql_expression=sql,
        notes=reasoning,
    )

    trace = AgentTrace(
        column_name=col.name,
        agent="mapping",
        tool_calls=tool_calls_log,
        final_target=target,
        final_confidence=round(confidence, 4),
        reasoning_summary=reasoning,
    )

    return mapping, trace


async def _run_async(
    columns: list[ColumnProfile],
    source_table: str,
    source_name: str,
    concurrency: int,
    system_prompt: str,
    max_tool_calls: int,
) -> list[tuple[ColumnMapping, AgentTrace]]:
    """Run all columns concurrently, capped at `concurrency` in flight."""
    import anthropic

    client = anthropic.Anthropic()
    semaphore = asyncio.Semaphore(concurrency)

    async def _guarded(col: ColumnProfile):
        async with semaphore:
            return await _map_one_column(client, col, source_table, source_name, system_prompt, max_tool_calls)

    return await asyncio.gather(*[_guarded(c) for c in columns])


def run_mapping_agent(
    columns: list[ColumnProfile],
    source_table: str,
    source_name: str,
    all_profiles: list[ColumnProfile],
    is_empty_string_null: bool = True,
    concurrency: int = DEFAULT_CONCURRENCY,
    system_prompt_override: str | None = None,
    canonical_schema: str = "policy",
) -> tuple[list[ColumnMapping], list[AgentTrace]]:
    """Public entry point. Map a list of low-confidence columns via the agent loop.

    Args:
        columns:             the low-confidence columns to map.
        source_table:        table name for the ColumnMapping records.
        source_name:         logical source name — used to retrieve the
                              relevant few-shot example bank (MAP-4 Layer 1).
        all_profiles:        ALL columns in the table (registered for tool lookups).
        is_empty_string_null: PAS-L style empty-string nulls.
        concurrency:         max columns processed in parallel.
        system_prompt_override: MAP-4 Layer 2 — use this exact prompt text
                              instead of resolving the active/default one.
                              Only tools/tune_prompts.py's VALIDATE step
                              should pass this (scoring a not-yet-accepted
                              candidate); production callers leave it None.
        canonical_schema:     which canonical/registry.py schema key the
                              lookup_canonical/score_name_similarity/generate_sql
                              tools should search (see canonical/registry.py).

    Returns:
        (mappings, traces) — parallel lists.
    """
    # Register profiles so get_column_profile / generate_sql tools can see them
    register_profiles(all_profiles, is_empty_string_null, source_name=source_name, canonical_schema=canonical_schema)
    system_prompt = system_prompt_override or _active_system_prompt()
    max_tool_calls = _max_tool_calls()

    # asyncio.run() crashes if an event loop is already running (Jupyter, async
    # CI runners). Detect that case and fall back to nest_asyncio.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        results = asyncio.run(_run_async(columns, source_table, source_name, concurrency, system_prompt, max_tool_calls))
    else:
        import nest_asyncio
        nest_asyncio.apply()
        results = loop.run_until_complete(
            _run_async(columns, source_table, source_name, concurrency, system_prompt, max_tool_calls)
        )
    mappings = [m for m, _ in results]
    traces = [t for _, t in results]
    return mappings, traces