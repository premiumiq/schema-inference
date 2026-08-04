"""MAP-9 Layer 3 (analysis only) -- tool-call usage vs. outcome report.

Layers 0-2 tune rule weights, few-shot examples, and system prompts against
ground truth. Nothing looks at *which tools the agent calls, when, or how
well* -- this script closes that gap by joining tool_usage_history (written
by orchestrator.py's run_mapping(), see metamodel/store.py's
record_tool_usage()) against mapping_history.verdict (written by
scripts/score_mappings.py once a run is scored).

Report sections (see docs/llm-provider-abstraction-and-tool-tuning-plan.md,
MAP-9 Step 2, for the full rationale):

  1. Per-tool marginal value -- for columns grouped by profile signature
     (same flag set few_shot.py's retrieval key uses: inferred_type,
     is_id_column, is_coded_column, is_cents_integer, date_format), compare
     accuracy (TP/TN vs. FP/FN/WRONG_TARGET) between columns where a given
     tool was called vs. wasn't. Layer 0's grid-search methodology applied
     to a categorical action space (tool subset) instead of continuous
     weights.
  2. Call efficiency -- % of columns whose trace hit
     mapping_agent.max_tool_calls_per_column (forced cutoff, investigation
     truncated), and count of duplicate same-tool-same-input calls within a
     single trace (wasted round trips).
  3. Under-triggering -- profile-signature/tool combinations where NOT
     calling that tool correlates with a higher false-positive/false-
     negative/wrong-target rate than calling it. This is the signal a human
     would act on to populate agent_config.yml's
     mapping_agent.mandatory_tool_triggers (see mapping_agent.py's
     _mandatory_tool_triggers() scaffold -- not yet wired into the tool-use
     loop).

This is a report generator only -- no --apply, no auto-write. Per the plan
doc's sequencing, the point of this step is to validate the signal is
actually useful before wiring any feedback loop back into the running
system. Handles the case where tool_usage_history is empty (this repo's CI
and most dev environments have no long history of scored live-agent runs)
without crashing.

Usage:
    python tools/analyze_tool_usage.py --source-name pasl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from schema_inference.agents.mapping_agent import _max_tool_calls
from schema_inference.metamodel.few_shot import _SIGNATURE_KEYS
from schema_inference.metamodel.store import open_store

# scripts/score_mappings.py's verdict vocabulary (AggregateMetrics.verdict).
_CORRECT_VERDICTS = {"TP", "TN"}
_ERROR_VERDICTS = {"FP", "FN", "WRONG_TARGET"}
_SCORED_VERDICTS = _CORRECT_VERDICTS | _ERROR_VERDICTS


# ── Loading + joining ──────────────────────────────────────────────────────────

def _profile_signature_key(sig: dict | None) -> tuple:
    """Same grouping key few_shot.py's retrieval uses (_SIGNATURE_KEYS) --
    one profile-signature scheme for the whole self-tuning system, not a
    second one invented here."""
    if not sig:
        return (("__no_signature__", True),)
    return tuple((k, sig.get(k)) for k in _SIGNATURE_KEYS)


def _group_label(key: tuple) -> str:
    return ",".join(f"{k}={v}" for k, v in key)


def _load_joined_rows(source_name: str) -> list[dict]:
    """Join tool_usage_history against mapping_history on (run_id,
    source_column). Returns one dict per (run_id, source_column) that has at
    least one tool_usage_history row:
        {run_id, source_column, verdict, profile_signature, calls}
    calls is that trace's tool_usage_history rows, sorted by call_order.

    Best-effort like the rest of the metamodel layer: returns [] if the
    store can't be opened or has no matching rows, never raises.
    """
    try:
        store = open_store()
    except Exception:
        return []
    if not store:
        return []
    try:
        history = store.get_mapping_history(source_name, limit=100000)
        tool_rows = store.get_tool_usage_history(source_name, limit=1000000)
    except Exception:
        return []
    finally:
        store.close()

    verdict_by_key: dict[tuple, str | None] = {}
    sig_by_key: dict[tuple, dict | None] = {}
    for r in history:
        key = (r["run_id"], r["source_column"])
        # mapping_history can carry multiple rows per key across re-runs;
        # rows are not guaranteed ordered here, so last-write-wins is
        # approximate but fine for a report -- exact semantics matter for
        # tuning decisions (Step 3), not for this diagnostic pass.
        verdict_by_key[key] = r.get("verdict")
        if r.get("profile_signature_json"):
            sig_by_key[key] = json.loads(r["profile_signature_json"])

    calls_by_key: dict[tuple, list[dict]] = defaultdict(list)
    for row in tool_rows:
        key = (row["run_id"], row["source_column"])
        calls_by_key[key].append(row)

    joined = []
    for key, calls in calls_by_key.items():
        calls.sort(key=lambda r: r["call_order"])
        joined.append({
            "run_id": key[0],
            "source_column": key[1],
            "verdict": verdict_by_key.get(key),
            "profile_signature": sig_by_key.get(key),
            "calls": calls,
        })
    return joined


# ── Section 1 + 3: shared called-vs-not-called split ──────────────────────────

def _grouped_tool_splits(joined: list[dict]) -> list[tuple[str, str, list[dict], list[dict]]]:
    """For every (profile-signature group, tool) pair seen in scored rows,
    split that group's columns into (called, not_called). Shared by the
    marginal-value and under-triggering sections so they run one grouping,
    not two independent ones, and so their group labels/definitions can
    never drift apart from each other."""
    scored = [r for r in joined if r["verdict"] in _SCORED_VERDICTS]
    by_group: dict[tuple, list[dict]] = defaultdict(list)
    for r in scored:
        by_group[_profile_signature_key(r["profile_signature"])].append(r)

    all_tools = sorted({c["tool_name"] for r in scored for c in r["calls"]})

    splits = []
    for group_key, rows in by_group.items():
        if len(rows) < 2:
            continue  # need at least 2 columns in a group to compare anything
        for tool in all_tools:
            called = [r for r in rows if any(c["tool_name"] == tool for c in r["calls"])]
            not_called = [r for r in rows if not any(c["tool_name"] == tool for c in r["calls"])]
            if called and not_called:
                splits.append((_group_label(group_key), tool, called, not_called))
    return splits


def _per_tool_marginal_value(joined: list[dict]) -> list[dict]:
    results = []
    for group, tool, called, not_called in _grouped_tool_splits(joined):
        called_acc = sum(r["verdict"] in _CORRECT_VERDICTS for r in called) / len(called)
        not_called_acc = sum(r["verdict"] in _CORRECT_VERDICTS for r in not_called) / len(not_called)
        results.append({
            "group": group,
            "tool": tool,
            "called_acc": called_acc,
            "called_n": len(called),
            "not_called_acc": not_called_acc,
            "not_called_n": len(not_called),
            "delta": called_acc - not_called_acc,
        })
    results.sort(key=lambda r: -abs(r["delta"]))
    return results


def _under_triggering(joined: list[dict], min_delta: float = 0.20) -> list[dict]:
    """Groups where skipping `tool` correlates with a higher error rate than
    calling it, by at least min_delta. This is the raw material for a
    mandatory_tool_triggers entry (agent_config.yml) -- see the plan doc's
    Step 3 and mapping_agent.py's _mandatory_tool_triggers() scaffold."""
    results = []
    for group, tool, called, not_called in _grouped_tool_splits(joined):
        error_with = sum(r["verdict"] in _ERROR_VERDICTS for r in called) / len(called)
        error_without = sum(r["verdict"] in _ERROR_VERDICTS for r in not_called) / len(not_called)
        delta = error_without - error_with
        if delta >= min_delta:
            results.append({
                "group": group,
                "tool": tool,
                "error_rate_with": error_with,
                "n_with": len(called),
                "error_rate_without": error_without,
                "n_without": len(not_called),
                "delta": delta,
            })
    results.sort(key=lambda r: -r["delta"])
    return results


# ── Section 2: call efficiency ─────────────────────────────────────────────────

def _call_efficiency(joined: list[dict]) -> dict:
    max_calls = _max_tool_calls()
    total = len(joined)
    cutoff = sum(1 for r in joined if len(r["calls"]) >= max_calls)

    duplicate_count = 0
    for r in joined:
        seen: Counter = Counter()
        for c in r["calls"]:
            seen[(c["tool_name"], c["inputs_json"])] += 1
        duplicate_count += sum(n - 1 for n in seen.values() if n > 1)

    return {
        "total": total,
        "max_tool_calls_per_column": max_calls,
        "cutoff_count": cutoff,
        "cutoff_pct": (cutoff / total) if total else 0.0,
        "duplicate_count": duplicate_count,
    }


# ── Report ──────────────────────────────────────────────────────────────────────

def run_tool_usage_analysis(source_name: str = "pasl") -> dict:
    """Callable core (main()'s CLI below is a thin wrapper). Prints the
    report and returns a structured dict of the same data, in the same
    "print for terminal users, return dict for programmatic callers" style
    as tools/tune_rule_weights.py's run_layer0_tuning()."""
    joined = _load_joined_rows(source_name)

    if not joined:
        print(f"No tool_usage_history found for source '{source_name}'.")
        print(
            "Run the agent pipeline at least once with --agent (and metamodel "
            "recording enabled, the default) to populate tool_usage_history, "
            "then re-run this script. Nothing to analyze yet."
        )
        return {"source_name": source_name, "rows": 0}

    print(f"Loaded {len(joined)} tool-call trace(s) for source '{source_name}'.\n")

    marginal = _per_tool_marginal_value(joined)
    print("=== Per-tool marginal value (accuracy with vs. without each tool, by profile-signature group) ===")
    if not marginal:
        print("  (not enough grouped data yet -- need >=1 scored column with the tool called "
              "and >=1 without, in the same profile-signature group)")
    for row in marginal:
        print(
            f"  group=[{row['group']}]\n"
            f"    tool={row['tool']:<24} "
            f"called_acc={row['called_acc']:.2f} (n={row['called_n']})  "
            f"not_called_acc={row['not_called_acc']:.2f} (n={row['not_called_n']})  "
            f"delta={row['delta']:+.2f}"
        )

    eff = _call_efficiency(joined)
    print("\n=== Call efficiency ===")
    print(f"  max_tool_calls_per_column (config): {eff['max_tool_calls_per_column']}")
    print(
        f"  columns that hit the forced cutoff: {eff['cutoff_count']}/{eff['total']} "
        f"({eff['cutoff_pct']:.1%})"
    )
    print(f"  duplicate same-tool-same-input calls (wasted round trips): {eff['duplicate_count']}")

    under = _under_triggering(joined)
    print("\n=== Under-triggering (skipping a tool correlates with a higher error rate) ===")
    if not under:
        print("  (no profile-signature/tool combination shows a clear skip-vs-error correlation "
              f">= {0.20:.0%} yet)")
    for row in under:
        print(
            f"  group=[{row['group']}]\n"
            f"    tool={row['tool']:<24} "
            f"error_rate_without={row['error_rate_without']:.2f} (n={row['n_without']})  "
            f"error_rate_with={row['error_rate_with']:.2f} (n={row['n_with']})  "
            f"delta={row['delta']:+.2f}"
        )
        print(
            "    -> candidate mandatory_tool_triggers entry (agent_config.yml, human-reviewed):\n"
            f"       profile_flags derived from group above, tool: {row['tool']}"
        )

    return {
        "source_name": source_name,
        "rows": len(joined),
        "marginal_value": marginal,
        "call_efficiency": eff,
        "under_triggering": under,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="MAP-9 Layer 3: analyze tool-call usage vs. mapping outcome "
                     "from tool_usage_history (report only, no --apply)."
    )
    parser.add_argument("--source-name", default="pasl", help="Logical source name (default: pasl)")
    args = parser.parse_args()

    run_tool_usage_analysis(source_name=args.source_name)


if __name__ == "__main__":
    main()
