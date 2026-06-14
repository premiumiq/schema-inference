"""InferenceOrchestrator — runs the full agent pipeline for one table.

Pipeline (per Shanth's spec):
  1. RuleEngine        — existing mapper.py rule pass (unchanged)
  2. MappingAgent      — replaces _run_llm_batch; per-column tool-use loop
  3. CriticAgent       — adversarial review of hard + below-floor columns   [TODO]
  4. SQLAgent          — SQL expression finalization                         [TODO]
  5. EvaluatorAgent    — demo/CI only; wraps score_mappings.py               [TODO]

Currently wires steps 1 + 2. Steps 3-5 are added incrementally; the orchestrator
is structured so they slot in without reshaping the flow.

Produces an AgentMappingRun: the final MappingProposal plus per-column AgentTraces.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime

from ..mapper import (
    DEFAULT_LLM_THRESHOLD,
    _CDC_RE,
    _deduplicate,
    _rule_map_column,
)
from ..models import (
    AgentMappingRun,
    AgentTrace,
    ColumnMapping,
    MappingProposal,
    TableProfile,
)
from ..canonical.policy import CANONICAL_FIELDS
from .mapping_agent import run_mapping_agent


def run_mapping(
    table: TableProfile,
    source_name: str,
    llm_threshold: float = DEFAULT_LLM_THRESHOLD,
    use_agent: bool = True,
    concurrency: int = 10,
) -> AgentMappingRun:
    """Run the agent mapping pipeline for one table.

    Args:
        table:          TableProfile from the profiler.
        source_name:    Logical source name (for the proposal header).
        llm_threshold:  Columns with rule confidence < this go to the MappingAgent.
        use_agent:      If False, skip the agent pass (rule-only — useful for the
                        "before" half of the demo comparison).
        concurrency:    Max columns processed in parallel by the agent.

    Returns:
        AgentMappingRun with the final proposal and all agent traces.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now()
    t0 = time.perf_counter()

    # ── Split off CDC metadata columns (never mapped) ────────────────────────
    business_cols = []
    excluded_metadata: list[str] = []
    for col in table.columns:
        if _CDC_RE.match(col.name):
            excluded_metadata.append(col.name)
        else:
            business_cols.append(col)

    # ── Step 1: Rule pass (existing logic, unchanged) ────────────────────────
    rule_results: dict[str, ColumnMapping] = {}
    for col in business_cols:
        m = _rule_map_column(col, table.is_empty_string_null)
        m.source_table = table.name
        rule_results[col.name] = m

    rule_pass_count = len(rule_results)

    # ── Step 2: MappingAgent on low-confidence columns ───────────────────────
    low_conf_cols = [
        col for col in business_cols
        if rule_results[col.name].confidence < llm_threshold
    ]

    traces: list[AgentTrace] = []
    agent_results: dict[str, ColumnMapping] = {}

    if use_agent and low_conf_cols:
        agent_mappings, agent_traces = run_mapping_agent(
            columns=low_conf_cols,
            source_table=table.name,
            all_profiles=business_cols,
            is_empty_string_null=table.is_empty_string_null,
            concurrency=concurrency,
        )
        for m in agent_mappings:
            agent_results[m.source_column] = m
        traces.extend(agent_traces)

    agent_pass_count = len(agent_results)

    # ── Merge: agent result wins when it improves confidence ─────────────────
    merged: list[ColumnMapping] = []
    for col in business_cols:
        rule_m = rule_results[col.name]
        agent_m = agent_results.get(col.name)
        if agent_m and agent_m.confidence >= rule_m.confidence:
            merged.append(agent_m)
        else:
            merged.append(rule_m)

    # ── Step 3: CriticAgent — adversarial review of hard/below-floor columns ──
    critic_overrides = 0
    if use_agent:
        from .critic_agent import run_critic_agent
        profiles_by_name = {c.name: c for c in business_cols}
        merged, critic_traces, critic_overrides = run_critic_agent(
            merged, profiles_by_name
        )
        traces.extend(critic_traces)

    # ── Dedup + assemble proposal (existing logic) ───────────────────────────
    final_mappings = _deduplicate(merged)
    unmapped = [m.source_column for m in final_mappings if m.target_field is None]
    mapped_targets = {m.target_field for m in final_mappings if m.target_field}
    missing = [
        f.name for f in CANONICAL_FIELDS
        if f.required and f.name not in mapped_targets
    ]

    proposal = MappingProposal(
        source_name=source_name,
        table_name=table.name,
        mappings=final_mappings,
        unmapped_columns=unmapped,
        missing_standard_fields=missing,
        excluded_metadata_columns=excluded_metadata,
    )

    duration = time.perf_counter() - t0

    return AgentMappingRun(
        run_id=run_id,
        source_name=source_name,
        table_name=table.name,
        proposal=proposal,
        traces=traces,
        rule_pass_count=rule_pass_count,
        agent_pass_count=agent_pass_count,
        critic_overrides=critic_overrides,
        eval_score=None,             # set by EvaluatorAgent when added
        started_at=started_at,
        duration_seconds=round(duration, 2),
    )