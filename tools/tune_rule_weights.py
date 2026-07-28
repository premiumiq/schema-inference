"""MAP-4 Layer 0 — rule-engine weight tuner.

Grid-searches the rule engine's confidence weights
(name_sim, type_compat, pattern_bonus — see mapper.py:_compute_confidence)
against the ground-truth catalog, minimizing mean_loss (scripts/score_mappings.py,
MAP-2). No LLM calls — pure numeric optimization over a closed-form scoring
function, so this is cheap, deterministic, and the only Layer-0/Layer-1/Layer-2
tier the self-tuning plan calls "real gradient descent" rather than a
textual/LLM-driven analog. See docs/self-tuning-mapper-agent-plan.md (Layer 0).

For every column, changing the weights can change which canonical field wins
(not just the winning field's score), so each grid point re-runs the rule
engine's full per-column argmax (mapper._rule_map_column) rather than just
re-weighting a saved proposal's stored scalars.

Also prints an llm_threshold SENSITIVITY report (informational only, not
auto-applied): under the winning weights, how many columns would route to
the MappingAgent at each candidate threshold. Auto-tuning the threshold
itself would require live agent answers for columns never historically
routed to it — out of scope for a free, deterministic Layer-0 pass; a human
acts on this report by hand if a threshold change looks worthwhile.

Usage:
    python tools/tune_rule_weights.py                    # dry run, prints report only
    python tools/tune_rule_weights.py --apply             # writes the winning weights to agent_config.yml
    python tools/tune_rule_weights.py --source-name pasl --data-file path/to/file.dat
    python tools/tune_rule_weights.py --step 0.02         # finer grid (slower)
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from schema_inference.mapper import _CDC_RE, _deduplicate, _rule_map_column
from schema_inference.profiler import profile_file

import score_mappings as scorer  # noqa: E402  (sys.path adjusted above)

AGENT_CONFIG_PATH = _REPO_ROOT / "schema_inference" / "agent_config.yml"
DEFAULT_DATA_FILE = {
    "pasl": _REPO_ROOT / "examples" / "insurance" / "test_data" / "pasl_policy.dat",
    "pasm": _REPO_ROOT / "examples" / "insurance" / "test_data" / "pasm_policy.dat",
}

THRESHOLD_CANDIDATES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]


# ── Weight grid ────────────────────────────────────────────────────────────────

def _weight_grid(step: float) -> list[tuple[float, float, float]]:
    """Triangular grid over the simplex alpha+beta+gamma=1, alpha,beta,gamma>=0."""
    grid: list[tuple[float, float, float]] = []
    n = round(1.0 / step)
    for i in range(n + 1):
        alpha = round(i * step, 4)
        for j in range(n + 1 - i):
            beta = round(j * step, 4)
            gamma = round(1.0 - alpha - beta, 4)
            if gamma < -1e-9:
                continue
            gamma = max(gamma, 0.0)
            grid.append((alpha, beta, gamma))
    return grid


# ── Rule-only proposal under a weight tuple ───────────────────────────────────

def _build_rule_proposal(
    table,
    source_name: str,
    weights: tuple[float, float, float] | None,
) -> dict:
    business_cols = [c for c in table.columns if not _CDC_RE.match(c.name)]
    excluded = [c.name for c in table.columns if _CDC_RE.match(c.name)]

    mappings = []
    for col in business_cols:
        m = _rule_map_column(col, table.is_empty_string_null, weights=weights, source_name=source_name)
        m.source_table = table.name
        mappings.append(m)

    final, contested = _deduplicate(mappings)
    unmapped = [m.source_column for m in final if m.target_field is None]

    return {
        "source_name": source_name,
        "table_name": table.name,
        "mappings": [m.model_dump() for m in final],
        "unmapped_columns": unmapped,
        "missing_standard_fields": [],
        "excluded_metadata_columns": excluded,
        "contested_mappings": contested,
    }


def _score_proposal_dict(
    proposal: dict, source_name: str, silent: bool = False
) -> "scorer.AggregateMetrics":
    """silent=True suppresses score()'s metrics print (quiet=True only skips
    the per-column table — score() always prints the metrics block regardless).
    Needed because the grid search calls this hundreds of times."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(proposal, tmp)
        tmp.close()
        kwargs = dict(
            proposal_path=Path(tmp.name),
            source_name=source_name,
            quiet=True,
            use_color=False,
        )
        if silent:
            with contextlib.redirect_stdout(io.StringIO()):
                return scorer.score(**kwargs)
        return scorer.score(**kwargs)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


# ── Config write-back (regex, preserves comments) ────────────────────────────

def _write_weights_to_config(
    weights: tuple[float, float, float], config_path: Path, source_name: str
) -> None:
    """Write weights to rule_engine.weights_by_source.<source_name>, leaving
    every other source's entry and the global rule_engine.weights fallback
    untouched. Regex-based (not a full YAML round-trip) so the file's
    explanatory comments survive — a --apply on 'pasm' must not clobber
    'pasl's tuned weights or vice versa."""
    alpha, beta, gamma = weights
    text = config_path.read_text(encoding="utf-8")

    block = (
        f"    {source_name}:\n"
        f"      name_sim: {alpha:.4f}\n"
        f"      type_compat: {beta:.4f}\n"
        f"      pattern_bonus: {gamma:.4f}\n"
    )

    # Existing entry for this source -> replace it in place.
    source_block_re = re.compile(
        rf"^    {re.escape(source_name)}:\n(?:      \S.*\n)+", re.MULTILINE
    )
    if source_block_re.search(text):
        text = source_block_re.sub(lambda m: block, text, count=1)
        config_path.write_text(text, encoding="utf-8")
        return

    # No entry yet for this source, but weights_by_source: section exists -> append.
    wbs_re = re.compile(r"^  weights_by_source:\n", re.MULTILINE)
    if wbs_re.search(text):
        text = wbs_re.sub(lambda m: m.group(0) + block, text, count=1)
        config_path.write_text(text, encoding="utf-8")
        return

    # weights_by_source: section doesn't exist at all -> create it right
    # after the global rule_engine.weights block.
    weights_block_re = re.compile(r"^(  weights:\n(?:    \S.*\n)+)", re.MULTILINE)
    new_text, n = weights_block_re.subn(
        lambda m: m.group(1) + "  weights_by_source:\n" + block, text, count=1
    )
    if n == 0:
        raise ValueError(f"Could not find 'weights:' block to anchor weights_by_source in {config_path}")
    config_path.write_text(new_text, encoding="utf-8")


# ── Metamodel logging ─────────────────────────────────────────────────────────

def _log_tuning_run(
    source_name: str,
    table_name: str,
    weights_before: tuple[float, float, float],
    weights_after: tuple[float, float, float],
    loss_before: float,
    loss_after: float,
    applied: bool,
) -> None:
    try:
        from schema_inference.metamodel.store import open_store
    except ImportError:
        return
    store = open_store()
    if not store:
        return
    try:
        run_id = f"tune-layer0-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        store.record_loss_run(
            run_id=run_id,
            source_name=source_name,
            table_name=table_name,
            metrics={"mean_loss_before": loss_before, "mean_loss_after": loss_after},
            config_snapshot={
                "tuning_layer": 0,
                "weights_before": {"name_sim": weights_before[0], "type_compat": weights_before[1], "pattern_bonus": weights_before[2]},
                "weights_after": {"name_sim": weights_after[0], "type_compat": weights_after[1], "pattern_bonus": weights_after[2]},
                "applied": applied,
            },
        )
    finally:
        store.close()


# ── Callable core (CLI's main() below is a thin wrapper around this) ──────────

def _weights_dict(w: tuple[float, float, float]) -> dict:
    return {"name_sim": w[0], "type_compat": w[1], "pattern_bonus": w[2]}


def _metrics_dict(m: "scorer.AggregateMetrics") -> dict:
    return {"mean_loss": m.mean_loss, "f1": m.f1, "hard_f1": m.hard_f1}


def run_layer0_tuning(
    source_name: str = "pasl",
    data_file: str | Path | None = None,
    step: float = 0.05,
    apply: bool = False,
    top: int = 5,
) -> dict:
    """MAP-4 Layer 0 tuning, callable from anywhere (CLI's main(), or the
    VS Code bridge's tuning.run_layer0). Every print() below is an
    intentional side effect for terminal users, unchanged from before this
    was extracted from main() — a non-CLI caller (bridge) uses the
    returned dict and ignores stdout, same as every other tool in this
    repo that prints informationally.

    Raises FileNotFoundError if no data file is found/given.
    """
    data_file = Path(data_file) if data_file else DEFAULT_DATA_FILE.get(source_name)
    if not data_file or not data_file.exists():
        raise FileNotFoundError(
            f"no data file found for source '{source_name}' (looked for {data_file}). "
            "Pass data_file explicitly."
        )

    print(f"Profiling {data_file} ...")
    profile = profile_file(data_file, source_name=source_name)
    table = profile.tables[0]
    print(f"  {table.row_count} rows | {len(table.columns)} columns\n")

    # Baseline = whatever's currently active (weights=None -> _rule_weights() ->
    # agent_config.yml's current rule_engine.weights, or the hardcoded fallback).
    from schema_inference.mapper import _rule_weights
    baseline_weights = _rule_weights(source_name)
    print(f"Baseline weights (currently active): "
          f"name_sim={baseline_weights[0]:.2f} type_compat={baseline_weights[1]:.2f} pattern_bonus={baseline_weights[2]:.2f}")
    baseline_proposal = _build_rule_proposal(table, source_name, weights=baseline_weights)
    baseline_metrics = _score_proposal_dict(baseline_proposal, source_name)
    print(f"Baseline: mean_loss={baseline_metrics.mean_loss:.4f}  f1={baseline_metrics.f1:.4f}  hard_f1={baseline_metrics.hard_f1:.4f}\n")

    print(f"Grid-searching weight simplex (step={step}) ...")
    grid = _weight_grid(step)
    results: list[tuple[tuple[float, float, float], "scorer.AggregateMetrics"]] = []
    for w in grid:
        proposal = _build_rule_proposal(table, source_name, weights=w)
        metrics = _score_proposal_dict(proposal, source_name, silent=True)
        results.append((w, metrics))

    results.sort(key=lambda r: (r[1].mean_loss, -r[1].f1))
    best_weights, best_metrics = results[0]

    print(f"\nEvaluated {len(grid)} weight combinations.\n")
    print(f"{'RANK':<5}{'NAME_SIM':>10}{'TYPE_COMPAT':>13}{'PATTERN':>10}{'MEAN_LOSS':>12}{'F1':>8}{'HARD_F1':>10}")
    for i, (w, m) in enumerate(results[:top], start=1):
        print(f"{i:<5}{w[0]:>10.3f}{w[1]:>13.3f}{w[2]:>10.3f}{m.mean_loss:>12.4f}{m.f1:>8.4f}{m.hard_f1:>10.4f}")

    print(f"\nBest: name_sim={best_weights[0]:.3f} type_compat={best_weights[1]:.3f} pattern_bonus={best_weights[2]:.3f}")
    print(f"  mean_loss: {baseline_metrics.mean_loss:.4f} -> {best_metrics.mean_loss:.4f}  "
          f"({'improved' if best_metrics.mean_loss < baseline_metrics.mean_loss else 'no change'})")
    print(f"  f1:        {baseline_metrics.f1:.4f} -> {best_metrics.f1:.4f}")
    print(f"  hard_f1:   {baseline_metrics.hard_f1:.4f} -> {best_metrics.hard_f1:.4f}")

    applied = False
    if apply:
        if best_metrics.mean_loss < baseline_metrics.mean_loss:
            _write_weights_to_config(best_weights, AGENT_CONFIG_PATH, source_name)
            # _rule_weights is @lru_cache'd — harmless for the CLI (a fresh
            # process every run) but a long-lived caller in the same process
            # (the VS Code bridge) would otherwise keep serving pre-apply
            # weights for the rest of the session.
            _rule_weights.cache_clear()
            print(f"\nApplied — wrote weights to {AGENT_CONFIG_PATH}")
            applied = True
        else:
            print("\napply=True given but best candidate does not improve on baseline; leaving agent_config.yml unchanged.")
    else:
        print("\nDry run (apply=False) — agent_config.yml not modified.")

    _log_tuning_run(
        source_name=source_name,
        table_name=table.name,
        weights_before=baseline_weights,
        weights_after=best_weights,
        loss_before=baseline_metrics.mean_loss,
        loss_after=best_metrics.mean_loss,
        applied=applied,
    )

    # ── llm_threshold sensitivity report (informational only) ────────────────
    print("\nllm_threshold sensitivity (under winning weights; informational, not auto-applied):")
    best_proposal = _build_rule_proposal(table, source_name, weights=best_weights)
    confidences = sorted(m["confidence"] for m in best_proposal["mappings"])
    print(f"{'THRESHOLD':>10}{'COLS ROUTED TO AGENT':>24}")
    for t in THRESHOLD_CANDIDATES:
        routed = sum(1 for c in confidences if c < t)
        print(f"{t:>10.2f}{routed:>24}")

    return {
        "source_name": source_name,
        "table_name": table.name,
        "baseline_weights": _weights_dict(baseline_weights),
        "baseline_metrics": _metrics_dict(baseline_metrics),
        "best_weights": _weights_dict(best_weights),
        "best_metrics": _metrics_dict(best_metrics),
        "applied": applied,
        "top_candidates": [
            {"weights": _weights_dict(w), **_metrics_dict(m)}
            for w, m in results[:top]
        ],
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MAP-4 Layer 0: tune rule-engine confidence weights against ground truth.")
    parser.add_argument("--source-name", default="pasl", help="Logical source name (default: pasl)")
    parser.add_argument("--data-file", default=None, help="Flat file to profile (default: schema_inference/test_data/pasl_policy.dat for pasl)")
    parser.add_argument("--step", type=float, default=0.05, help="Grid step size (default: 0.05; smaller = finer/slower)")
    parser.add_argument("--apply", action="store_true", help="Write the winning weights to agent_config.yml (default: dry run, report only)")
    parser.add_argument("--top", type=int, default=5, help="How many top candidates to print (default: 5)")
    args = parser.parse_args()

    try:
        run_layer0_tuning(
            source_name=args.source_name, data_file=args.data_file,
            step=args.step, apply=args.apply, top=args.top,
        )
    except FileNotFoundError as exc:
        sys.exit(f"Error: {exc}")


if __name__ == "__main__":
    main()
