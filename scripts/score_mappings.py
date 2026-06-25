"""Score schema inference mapping quality against ground truth (MAP-2).

Loads a MappingProposal (output of schema_inference map) and evaluates it
against ground_truth/{source}_schema_catalog.yml (+ {source}_value_catalog.json),
producing:

  - per-column correctness table (TP/TN/FP/FN/WRONG_TARGET)
  - precision / recall / F1 for canonical field mappings
  - extended_attributes accuracy (correctly unmapped columns)
  - hard-column subset performance
  - missing required field detection accuracy
  - a continuous per-column loss (calibration penalty + transformation
    correctness + hard-column weighting) and one aggregate mean_loss scalar —
    the loss function MAP-4's self-tuning agent will minimize.

Catalog discovery is keyed by source name (defaults to "pasl", the only
source with a ground-truth catalog today) so this scorer works for any
future client/source without code changes — just add
ground_truth/{source}_schema_catalog.yml.

Usage
-----
    python scripts/score_mappings.py <proposal_json> [options]

    python scripts/score_mappings.py schema_inference/registry/pasl/proposal_pasl_policy.json
    python scripts/score_mappings.py proposal.json --no-color --csv results.csv
    python scripts/score_mappings.py proposal.json --quiet   # summary only
    python scripts/score_mappings.py proposal.json --source-name broker_abc
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Windows consoles default to cp1252, which can't encode the box-drawing and
# checkmark characters used in the report output below. reconfigure() is a
# no-op failure (not a crash) when stdout isn't a real stream (e.g. piped
# into something that already set its own encoding) — guard and ignore.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ── Ground truth loaders ──────────────────────────────────────────────────────

GROUND_TRUTH_DIR = _REPO_ROOT / "ground_truth"
DEFAULT_SOURCE_NAME = "pasl"

# Back-compat constant — existing callers (evaluator_agent.py, CI) that pass
# this explicitly keep working unchanged.
CATALOG_PATH = GROUND_TRUTH_DIR / "pasl_schema_catalog.yml"


def _catalog_path_for(source_name: str) -> Path:
    return GROUND_TRUTH_DIR / f"{source_name}_schema_catalog.yml"


def _value_catalog_path_for(source_name: str) -> Path:
    return GROUND_TRUTH_DIR / f"{source_name}_value_catalog.json"


def _load_catalog(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_value_catalog(path: Path) -> dict:
    """Value catalog is optional — used only for the transformation-correctness
    check. Missing file just means that loss term is skipped (sql_correct=None
    for every column), not an error."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Proposal loader ───────────────────────────────────────────────────────────

def _load_proposal(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Loss function weights ─────────────────────────────────────────────────────
# Module-level constants for now — MAP-4 Layer 0 (numeric knob tuning) will
# fit these against ground truth instead of hand-setting them.

TARGET_WEIGHT = 1.0    # weight on target-field correctness (TP/TN vs FP/FN/WRONG_TARGET)
CALIB_WEIGHT  = 0.3    # weight on confidence calibration (Brier-style)
SQL_WEIGHT    = 1.0    # weight on transformation/SQL correctness
HARD_WEIGHT   = 2.0    # multiplier applied to the whole column loss when is_hard


# ── Transformation correctness ────────────────────────────────────────────────

def _check_transformation(sql_expression: str, value_entry: dict | None) -> bool | None:
    """Check the generated SQL applies the transformation the value catalog
    documents for this column. Returns None when not applicable (no
    transformation documented, or the column wasn't mapped to a target).

    Static substring/macro check, not a live SQL engine — most expressions
    use dbt-Jinja macros ({{ common_assets.* }}) that aren't valid standalone
    SQL, so this checks for the right operator/macro rather than executing
    the expression. Extend per-type as new transformation kinds appear in the
    value catalog.
    """
    if not value_entry:
        return None
    transformation = value_entry.get("transformation") or value_entry.get("derivation")
    if not transformation:
        return None

    col_type = value_entry.get("type")
    if col_type == "integer_cents":
        # Expect division by 100 (cents -> dollars)
        return "/" in sql_expression and "100" in sql_expression

    if col_type == "date":
        sql_lower = sql_expression.lower()
        return any(tok in sql_lower for tok in ("parse_compact_date", "parse_us_date", "cast("))

    # Unknown transformation kind — not checkable yet, don't penalize
    return None


# ── Column scoring ────────────────────────────────────────────────────────────

class ColumnScore(NamedTuple):
    column_name:         str
    gt_target:           str | None   # ground truth canonical target (None = extended_attributes)
    mapper_target:       str | None   # mapper's proposed target (None = extended_attributes)
    mapper_confidence:   float
    correct:             bool         # mapper target == gt target
    is_hard:             bool
    confidence_floor:    float | None # expected minimum confidence from catalog
    below_floor:         bool         # confidence < confidence_floor (false positives count more)
    verdict:             str          # "TP" | "TN" | "FP" | "FN" | "WRONG_TARGET"
    sql_correct:         bool | None  # None = not checkable; only meaningful when correct and mapped
    calibration_penalty: float        # (confidence - is_correct)^2
    loss:                float        # weighted per-column loss (see module weights above)


def _score_column(
    col_name: str,
    gt_entry: dict,
    mapper_target: str | None,
    mapper_confidence: float,
    mapper_sql: str,
    value_entry: dict | None,
) -> ColumnScore:
    gt_target  = gt_entry.get("canonical_target")     # None → should be extended_attributes
    conf_floor = gt_entry.get("confidence_floor")
    is_hard    = gt_entry.get("is_hard", False)

    below_floor = (
        conf_floor is not None
        and mapper_target is not None
        and mapper_confidence < conf_floor
    )

    # Determine correctness verdict
    if gt_target is None and mapper_target is None:
        verdict  = "TN"   # correctly left unmapped
        correct  = True
    elif gt_target is not None and mapper_target == gt_target:
        verdict  = "TP"   # correctly mapped
        correct  = True
    elif gt_target is not None and mapper_target is None:
        verdict  = "FN"   # should have been mapped but wasn't
        correct  = False
    elif gt_target is None and mapper_target is not None:
        verdict  = "FP"   # should be extended_attributes but was mapped
        correct  = False
    else:
        verdict  = "WRONG_TARGET"   # mapped but to wrong canonical field
        correct  = False

    # SQL correctness only meaningful when the target itself is right and mapped
    sql_correct = _check_transformation(mapper_sql, value_entry) if (correct and mapper_target) else None

    calibration_penalty = (mapper_confidence - (1.0 if correct else 0.0)) ** 2

    target_term = 0.0 if correct else 1.0
    sql_term    = 1.0 if sql_correct is False else 0.0
    hard_mult   = HARD_WEIGHT if is_hard else 1.0
    loss = hard_mult * (
        target_term * TARGET_WEIGHT
        + calibration_penalty * CALIB_WEIGHT
        + sql_term * SQL_WEIGHT
    )

    return ColumnScore(
        column_name=col_name,
        gt_target=gt_target,
        mapper_target=mapper_target,
        mapper_confidence=mapper_confidence,
        correct=correct,
        is_hard=is_hard,
        confidence_floor=conf_floor,
        below_floor=below_floor,
        verdict=verdict,
        sql_correct=sql_correct,
        calibration_penalty=round(calibration_penalty, 4),
        loss=round(loss, 4),
    )


# ── Missing field detection scoring ──────────────────────────────────────────

class MissingFieldScore(NamedTuple):
    field_name:          str
    in_gt_missing:       bool   # catalog says it should be missing
    in_mapper_missing:   bool   # mapper also declared it missing
    correct:             bool


def _score_missing_fields(
    gt_missing: list[dict],
    mapper_missing: list[str],
) -> list[MissingFieldScore]:
    gt_names     = {m["name"] for m in gt_missing}
    mapper_names = set(mapper_missing)
    all_names    = gt_names | mapper_names
    return [
        MissingFieldScore(
            field_name=name,
            in_gt_missing=name in gt_names,
            in_mapper_missing=name in mapper_names,
            correct=(name in gt_names) == (name in mapper_names),
        )
        for name in sorted(all_names)
    ]


# ── Aggregate metrics ─────────────────────────────────────────────────────────

def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


class AggregateMetrics(NamedTuple):
    total_columns:         int
    correct:               int
    tp:                    int    # correctly mapped
    tn:                    int    # correctly unmapped
    fp:                    int    # over-mapped (should be extended_attributes)
    fn:                    int    # under-mapped (should have canonical target)
    wrong_target:          int    # mapped but to wrong field
    precision:             float
    recall:                float
    f1:                    float
    ext_attr_accuracy:     float  # TN / (TN + FP)  for extended_attributes cols
    hard_precision:        float
    hard_recall:           float
    hard_f1:               float
    below_floor_count:     int    # mapped correctly but below expected confidence
    mean_loss:             float  # aggregate of per-column loss — MAP-4 minimizes this
    sql_correctness_rate:  float  # fraction of checkable transformations that were correct


def _compute_metrics(scores: list[ColumnScore]) -> AggregateMetrics:
    tp = sum(1 for s in scores if s.verdict == "TP")
    tn = sum(1 for s in scores if s.verdict == "TN")
    fp = sum(1 for s in scores if s.verdict == "FP")
    fn = sum(1 for s in scores if s.verdict == "FN")
    wt = sum(1 for s in scores if s.verdict == "WRONG_TARGET")

    precision = tp / (tp + fp + wt) if (tp + fp + wt) > 0 else 0.0
    recall    = tp / (tp + fn + wt) if (tp + fn + wt) > 0 else 0.0

    ext_total = tn + fp
    ext_acc   = tn / ext_total if ext_total > 0 else 1.0

    hard = [s for s in scores if s.is_hard]
    h_tp = sum(1 for s in hard if s.verdict == "TP")
    h_fp = sum(1 for s in hard if s.verdict in ("FP", "WRONG_TARGET"))
    h_fn = sum(1 for s in hard if s.verdict == "FN")
    h_wt = sum(1 for s in hard if s.verdict == "WRONG_TARGET")
    h_prec = h_tp / (h_tp + h_fp)     if (h_tp + h_fp) > 0 else 0.0
    h_rec  = h_tp / (h_tp + h_fn + h_wt) if (h_tp + h_fn + h_wt) > 0 else 0.0

    below_floor = sum(1 for s in scores if s.below_floor)

    mean_loss = sum(s.loss for s in scores) / len(scores) if scores else 0.0
    sql_checked = [s for s in scores if s.sql_correct is not None]
    sql_correctness_rate = (
        sum(1 for s in sql_checked if s.sql_correct) / len(sql_checked)
        if sql_checked else 1.0
    )

    return AggregateMetrics(
        total_columns=len(scores),
        correct=tp + tn,
        tp=tp, tn=tn, fp=fp, fn=fn, wrong_target=wt,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(_f1(precision, recall), 4),
        ext_attr_accuracy=round(ext_acc, 4),
        hard_precision=round(h_prec, 4),
        hard_recall=round(h_rec, 4),
        hard_f1=round(_f1(h_prec, h_rec), 4),
        below_floor_count=below_floor,
        mean_loss=round(mean_loss, 4),
        sql_correctness_rate=round(sql_correctness_rate, 4),
    )


# ── Console output ────────────────────────────────────────────────────────────

RESET  = "\033[0m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"


def _color(text: str, code: str, use_color: bool) -> str:
    return f"{code}{text}{RESET}" if use_color else text


def _verdict_color(verdict: str, use_color: bool) -> str:
    colors = {"TP": GREEN, "TN": GREEN, "FP": RED, "FN": RED, "WRONG_TARGET": YELLOW}
    return _color(verdict, colors.get(verdict, RESET), use_color)


def _print_column_table(
    scores: list[ColumnScore],
    use_color: bool,
    show_correct: bool,
) -> None:
    rows = scores if show_correct else [s for s in scores if not s.correct or s.below_floor]
    if not rows:
        print("  (all columns correctly mapped)\n")
        return

    hdr = (f"{'COLUMN':<22} {'GT TARGET':<24} {'MAPPER TARGET':<24} {'CONF':>6} "
           f"{'VERDICT':<14} {'SQL':>5} {'LOSS':>6} {'HARD':>5}")
    print(_color(hdr, BOLD, use_color))
    print("-" * 112)
    for s in rows:
        gt_str  = s.gt_target or "(extended_attributes)"
        mp_str  = s.mapper_target or "(extended_attributes)"
        flag    = " !" if s.below_floor else ("  H" if s.is_hard else "")
        conf_str = f"{s.mapper_confidence:.3f}"
        sql_str  = "—" if s.sql_correct is None else ("OK" if s.sql_correct else "BAD")
        sql_disp = sql_str if s.sql_correct is not False else _color(sql_str, RED, use_color)
        line = (
            f"{s.column_name:<22} {gt_str:<24} {mp_str:<24} "
            f"{conf_str:>6} {_verdict_color(s.verdict, use_color):<14} "
            f"{sql_disp:>5} {s.loss:>6.3f} {flag:>5}"
        )
        print(line)
    print()


def _print_metrics(m: AggregateMetrics, use_color: bool) -> None:
    def _bar(label: str, val: float, width: int = 30) -> str:
        filled = int(val * width)
        bar    = "█" * filled + "░" * (width - filled)
        pct    = f"{val * 100:5.1f}%"
        return f"  {label:<28} {bar}  {pct}"

    print(_color(f"\n{'─' * 60}", BOLD, use_color))
    print(_color("  MAPPING QUALITY METRICS", BOLD, use_color))
    print(_color(f"{'─' * 60}", BOLD, use_color))
    print(f"  Total columns scored:        {m.total_columns}")
    print(f"  Correct (TP + TN):           {m.correct}  "
          f"({m.tp} mapped correctly, {m.tn} correctly unmapped)")
    print(f"  Errors:  FP={m.fp}  FN={m.fn}  WRONG={m.wrong_target}")
    print(f"  Mean loss:                   {m.mean_loss:.4f}  "
          f"(SQL correctness: {m.sql_correctness_rate * 100:.1f}%)")
    print()
    print(_bar("Precision (mapped correct/all mapped)", m.precision))
    print(_bar("Recall    (GT-mapped cols found)", m.recall))
    print(_bar("F1                              ", m.f1))
    print(_bar("Ext-attrs accuracy (TN / TN+FP) ", m.ext_attr_accuracy))
    print()
    print(_color("  Hard columns:", CYAN, use_color))
    print(_bar("  Hard Precision", m.hard_precision))
    print(_bar("  Hard Recall   ", m.hard_recall))
    print(_bar("  Hard F1       ", m.hard_f1))
    if m.below_floor_count:
        print(f"\n  {_color(f'⚠  {m.below_floor_count} columns mapped correctly but below confidence floor', YELLOW, use_color)}")
    print(_color(f"{'─' * 60}\n", BOLD, use_color))


def _print_missing_fields(scores: list[MissingFieldScore], use_color: bool) -> None:
    if not scores:
        return
    print(_color("  MISSING REQUIRED FIELD DETECTION", BOLD, use_color))
    print(f"  {'FIELD':<30} {'IN GT':>6} {'DETECTED':>9} {'OK':>4}")
    print("  " + "-" * 55)
    for s in scores:
        ok   = _color("✓", GREEN, use_color) if s.correct else _color("✗", RED, use_color)
        gt   = "yes" if s.in_gt_missing else "no"
        det  = "yes" if s.in_mapper_missing else "no"
        print(f"  {s.field_name:<30} {gt:>6} {det:>9} {ok:>4}")
    correct = sum(1 for s in scores if s.correct)
    print(f"\n  {correct}/{len(scores)} missing fields correctly identified\n")


# ── CSV export ────────────────────────────────────────────────────────────────

def _write_csv(scores: list[ColumnScore], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "column_name", "gt_target", "mapper_target", "mapper_confidence",
            "verdict", "correct", "is_hard", "confidence_floor", "below_floor",
            "sql_correct", "calibration_penalty", "loss",
        ])
        for s in scores:
            w.writerow([
                s.column_name,
                s.gt_target or "",
                s.mapper_target or "",
                s.mapper_confidence,
                s.verdict,
                s.correct,
                s.is_hard,
                s.confidence_floor or "",
                s.below_floor,
                "" if s.sql_correct is None else s.sql_correct,
                s.calibration_penalty,
                s.loss,
            ])
    print(f"CSV saved → {path}")


# ── Metamodel wiring (MAP-1) ──────────────────────────────────────────────────

def _record_to_metamodel(
    run_id: str,
    source_name: str,
    table_name: str,
    scores: list[ColumnScore],
    metrics: AggregateMetrics,
) -> None:
    """Best-effort: update mapping_history verdicts and write a loss_runs row.
    Never raises — history is optional and must not affect scoring exit code."""
    try:
        from schema_inference.metamodel.store import open_store
    except ImportError:
        return

    store = open_store()
    if not store:
        return
    try:
        for s in scores:
            store.update_mapping_verdict(run_id, s.column_name, s.verdict)

        config_snapshot: dict = {}
        agent_config_path = _REPO_ROOT / "schema_inference" / "agent_config.yml"
        if agent_config_path.exists():
            with open(agent_config_path, encoding="utf-8") as f:
                config_snapshot = yaml.safe_load(f) or {}
        config_snapshot["loss_weights"] = {
            "target": TARGET_WEIGHT, "calib": CALIB_WEIGHT,
            "sql": SQL_WEIGHT, "hard": HARD_WEIGHT,
        }

        store.record_loss_run(
            run_id=run_id,
            source_name=source_name,
            table_name=table_name,
            metrics=metrics._asdict(),
            config_snapshot=config_snapshot,
        )
    finally:
        store.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def score(
    proposal_path: Path,
    catalog_path: Path | None = None,
    value_catalog_path: Path | None = None,
    source_name: str | None = None,
    quiet: bool = False,
    show_all: bool = False,
    use_color: bool = True,
    csv_path: Path | None = None,
    run_id: str | None = None,
) -> AggregateMetrics:
    """Score a MappingProposal JSON against ground truth.

    Args:
        proposal_path:       MappingProposal JSON file.
        catalog_path:         Schema catalog YAML. Defaults to
                              ground_truth/{source_name}_schema_catalog.yml.
        value_catalog_path:   Value catalog JSON. Defaults to
                              ground_truth/{source_name}_value_catalog.json.
                              Optional — missing file just skips the
                              transformation-correctness loss term.
        source_name:          Logical source name used to resolve the two
                              catalog paths above when not explicitly given.
                              Defaults to "pasl" (the only source with a
                              ground-truth catalog today).
        run_id:               If given, mapping_history verdicts and a
                              loss_runs row are recorded to the metamodel
                              store (MAP-1) under this run_id. No-op if the
                              store can't be opened.
    """
    resolved_source = source_name or DEFAULT_SOURCE_NAME
    if catalog_path is None:
        catalog_path = _catalog_path_for(resolved_source)
    if value_catalog_path is None:
        value_catalog_path = _value_catalog_path_for(resolved_source)

    catalog       = _load_catalog(catalog_path)
    value_catalog = _load_value_catalog(value_catalog_path)
    proposal      = _load_proposal(proposal_path)

    gt_columns:    dict[str, dict] = catalog.get("columns", {})
    gt_missing:    list[dict]      = catalog.get("missing_standard_fields", [])
    value_columns: dict[str, dict] = value_catalog.get("columns", {})

    # Build mapper lookup: source_column → (target_field, confidence, sql_expression)
    mapper_map: dict[str, tuple[str | None, float, str]] = {}
    for m in proposal.get("mappings", []):
        mapper_map[m["source_column"]] = (
            m.get("target_field"), m.get("confidence", 0.0), m.get("sql_expression", ""),
        )

    # Score each column in the ground truth catalog
    scores: list[ColumnScore] = []
    for col_name, gt_entry in gt_columns.items():
        mapper_target, mapper_conf, mapper_sql = mapper_map.get(col_name, (None, 0.0, ""))
        scores.append(_score_column(
            col_name, gt_entry, mapper_target, mapper_conf, mapper_sql,
            value_columns.get(col_name),
        ))

    # Score missing field detection
    mapper_missing = proposal.get("missing_standard_fields", [])
    missing_scores = _score_missing_fields(gt_missing, mapper_missing)

    metrics = _compute_metrics(scores)

    if not quiet:
        print(_color("\n  PER-COLUMN DETAIL", BOLD, use_color))
        _print_column_table(scores, use_color=use_color, show_correct=show_all)
    _print_metrics(metrics, use_color=use_color)
    if not quiet:
        _print_missing_fields(missing_scores, use_color=use_color)

    if csv_path:
        _write_csv(scores, csv_path)

    if run_id:
        _record_to_metamodel(
            run_id=run_id,
            source_name=resolved_source,
            table_name=proposal.get("table_name", ""),
            scores=scores,
            metrics=metrics,
        )

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a MappingProposal JSON against its ground truth catalog."
    )
    parser.add_argument("proposal", help="MappingProposal JSON file (output of schema_inference map)")
    parser.add_argument(
        "--source-name", default=None, metavar="NAME",
        help=f"Logical source name for catalog lookup (default: {DEFAULT_SOURCE_NAME!r})",
    )
    parser.add_argument(
        "--catalog", default=None,
        help="Path to schema catalog YAML (default: ground_truth/{source}_schema_catalog.yml)",
    )
    parser.add_argument(
        "--value-catalog", dest="value_catalog", default=None,
        help="Path to value catalog JSON (default: ground_truth/{source}_value_catalog.json)",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true",
        help="Print summary metrics only (skip per-column table)",
    )
    parser.add_argument(
        "--all", dest="show_all", action="store_true",
        help="Show all columns, not just errors and below-floor warnings",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable ANSI color output",
    )
    parser.add_argument(
        "--csv", dest="csv_path", default=None,
        help="Write per-column results to a CSV file",
    )
    parser.add_argument(
        "--fail-below", type=float, default=None, metavar="F1",
        help="Exit with code 1 if overall F1 drops below this threshold (e.g. 0.80)",
    )
    parser.add_argument(
        "--fail-above", type=float, default=None, metavar="LOSS",
        help="Exit with code 1 if mean_loss rises above this threshold (e.g. 0.20)",
    )

    args = parser.parse_args()
    proposal_path = Path(args.proposal)
    if not proposal_path.exists():
        sys.exit(f"Error: file not found: {proposal_path}")

    catalog_path = Path(args.catalog) if args.catalog else None
    if catalog_path is not None and not catalog_path.exists():
        sys.exit(f"Error: catalog not found: {catalog_path}")

    value_catalog_path = Path(args.value_catalog) if args.value_catalog else None
    csv_path = Path(args.csv_path) if args.csv_path else None

    metrics = score(
        proposal_path=proposal_path,
        catalog_path=catalog_path,
        value_catalog_path=value_catalog_path,
        source_name=args.source_name,
        quiet=args.quiet,
        show_all=args.show_all,
        use_color=not args.no_color,
        csv_path=csv_path,
    )

    failed = False
    if args.fail_below is not None and metrics.f1 < args.fail_below:
        print(f"FAIL: F1={metrics.f1:.4f} is below threshold {args.fail_below}")
        failed = True
    if args.fail_above is not None and metrics.mean_loss > args.fail_above:
        print(f"FAIL: mean_loss={metrics.mean_loss:.4f} is above threshold {args.fail_above}")
        failed = True
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
