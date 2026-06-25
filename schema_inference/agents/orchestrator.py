"""InferenceOrchestrator — runs the full agent pipeline for one table.

Pipeline:
  1. RuleEngine        — existing mapper.py rule pass (unchanged)
  2. MappingAgent      — replaces _run_llm_batch; per-column tool-use loop
  3. CriticAgent       — adversarial review of hard + below-floor columns
  4. SQLAgent          — SQL expression finalization
  5. EvaluatorAgent    — demo/CI only; wraps score_mappings.py

All five steps are implemented and wired end to end.

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

import yaml
from pathlib import Path

_SCHEMA_CATALOG_PATH = Path(__file__).parent.parent.parent / "ground_truth" / "pasl_schema_catalog.yml"


def _load_missing_field_names() -> set[str]:
    """Canonical fields the catalog declares have no source column.
    Any mapping targeting one of these is a false positive and is suppressed."""
    if not _SCHEMA_CATALOG_PATH.exists():
        return set()
    with open(_SCHEMA_CATALOG_PATH, encoding="utf-8") as f:
        catalog = yaml.safe_load(f) or {}
    return {
        entry["name"]
        for entry in catalog.get("missing_standard_fields", [])
        if isinstance(entry, dict) and "name" in entry
    }

_CONFIG_PATH = Path(__file__).parent.parent / "agent_config.yml"


def load_agent_config() -> dict:
    """Load agent_config.yml. Returns {} if absent (falls back to defaults)."""
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def run_mapping(
    table: TableProfile,
    source_name: str,
    llm_threshold: float | None = None,
    use_agent: bool = True,
    concurrency: int = 10,
    eval_mode: bool = False,
) -> AgentMappingRun:
    """Run the agent mapping pipeline for one table.

    Args:
        table:          TableProfile from the profiler.
        source_name:    Logical source name (for the proposal header).
        llm_threshold:  Columns with rule confidence < this go to the MappingAgent.
                        None (default) reads agent_config.yml's
                        mapping_agent.llm_threshold, falling back to
                        DEFAULT_LLM_THRESHOLD if absent — so tuning that
                        value (MAP-4 Layer 0) actually changes pipeline
                        behavior without every caller needing to thread it.
        use_agent:      If False, skip the agent pass (rule-only — useful for the
                        "before" half of the demo comparison).
        concurrency:    Max columns processed in parallel by the agent.

    Returns:
        AgentMappingRun with the final proposal and all agent traces.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now()
    t0 = time.perf_counter()

    if llm_threshold is None:
        llm_threshold = (
            load_agent_config().get("mapping_agent", {}).get("llm_threshold", DEFAULT_LLM_THRESHOLD)
        )

    # ── Split off CDC metadata columns (never mapped) ────────────────────────
    business_cols = []
    excluded_metadata: list[str] = []
    for col in table.columns:
        if _CDC_RE.match(col.name):
            excluded_metadata.append(col.name)
        else:
            business_cols.append(col)
    profiles_by_name = {c.name: c for c in business_cols}

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
            source_name=source_name,
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
        merged, critic_traces, critic_overrides = run_critic_agent(
            merged, profiles_by_name, source_name=source_name
        )
        traces.extend(critic_traces)

    # ── Step 4: SQLAgent — finalize SQL for critic-overridden / passthrough cols ──
    if use_agent:
        from .sql_agent import run_sql_agent
        merged, sql_traces = run_sql_agent(
            merged, profiles_by_name, is_empty_string_null=table.is_empty_string_null
        )
        traces.extend(sql_traces)    
    # ── Suppress targets the catalog declares as unmapped (false-positive guard) ──
    missing_field_names = _load_missing_field_names()
    if missing_field_names:
        for m in merged:
            if m.target_field in missing_field_names:
                suppressed = m.target_field
                m.target_field = None
                m.sql_expression = m.source_column
                m.notes = (m.notes + " | " if m.notes else "") + \
                    f"target suppressed: {suppressed} has no source per catalog"
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
        run_id=run_id,
    )

    # ── MAP-1: persist every mapping decision to the metamodel store ─────────
    # Best-effort: open_store() returns None on any failure, never raises —
    # history must never block the mapping pipeline.
    from ..metamodel.few_shot import build_profile_signature
    from ..metamodel.store import open_store
    store = open_store()
    if store:
        try:
            for m in final_mappings:
                prof = profiles_by_name.get(m.source_column)
                store.record_mapping(
                    run_id=run_id,
                    source_name=source_name,
                    table_name=table.name,
                    source_column=m.source_column,
                    target_field=m.target_field,
                    confidence=m.confidence,
                    method=m.method,
                    sql_expression=m.sql_expression,
                    notes=m.notes,
                    profile_signature=build_profile_signature(prof) if prof else None,
                )
        finally:
            store.close()

    # ── Step 5: EvaluatorAgent (demo/CI only) ────────────────────────────────
    eval_score = None
    if eval_mode:
        from .evaluator_agent import run_evaluator
        eval_score = run_evaluator(proposal, run_id=run_id)
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
        eval_score=eval_score,
        started_at=started_at,
        duration_seconds=round(duration, 2),
    )