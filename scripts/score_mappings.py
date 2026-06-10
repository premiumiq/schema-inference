"""Score schema inference mapping quality against PAS-L ground truth.

Loads a MappingProposal (output of schema_inference map) and evaluates it
against ground_truth/pasl_schema_catalog.yml, producing:

  - per-column correctness table
  - precision / recall / F1 for canonical field mappings
  - extended_attributes accuracy (correctly unmapped columns)
  - hard-column subset performance
  - missing required field detection accuracy

Usage
-----
    python scripts/score_mappings.py <proposal_json> [options]

    python scripts/score_mappings.py schema_inference/registry/pasl/proposal_pasl_policy.json
    python scripts/score_mappings.py proposal.json --no-color --csv results.csv
    python scripts/score_mappings.py proposal.json --quiet   # summary only
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

# ── Ground truth loader ───────────────────────────────────────────────────────

CATALOG_PATH = Path(__file__).parent.parent / "ground_truth" / "pasl_schema_catalog.yml"


def _load_catalog(path: Path = CATALOG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Proposal loader ───────────────────────────────────────────────────────────

def _load_proposal(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Column scoring ────────────────────────────────────────────────────────────

class ColumnScore(NamedTuple):
    column_name:        str
    gt_target:          str | None   # ground truth canonical target (None = extended_attributes)
    mapper_target:      str | None   # mapper's proposed target (None = extended_attributes)
    mapper_confidence:  float
    correct:            bool         # mapper target == gt target
    is_hard:            bool
    confidence_floor:   float | None # expected minimum confidence from catalog
    below_floor:        bool         # confidence < confidence_floor (false positives count more)
    verdict:            str          # "TP" | "TN" | "FP" | "FN" | "WRONG_TARGET"


def _score_column(
    col_name: str,
    gt_entry: dict,
    mapper_target: str | None,
    mapper_confidence: float,
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

    hdr = f"{'COLUMN':<22} {'GT TARGET':<24} {'MAPPER TARGET':<24} {'CONF':>6} {'VERDICT':<14} {'HARD':>5}"
    print(_color(hdr, BOLD, use_color))
    print("-" * 100)
    for s in rows:
        gt_str  = s.gt_target or "(extended_attributes)"
        mp_str  = s.mapper_target or "(extended_attributes)"
        flag    = " !" if s.below_floor else ("  H" if s.is_hard else "")
        conf_str = f"{s.mapper_confidence:.3f}"
        line = (
            f"{s.column_name:<22} {gt_str:<24} {mp_str:<24} "
            f"{conf_str:>6} {_verdict_color(s.verdict, use_color):<14} {flag:>5}"
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
            ])
    print(f"CSV saved → {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def score(
    proposal_path: Path,
    catalog_path: Path = CATALOG_PATH,
    quiet: bool = False,
    show_all: bool = False,
    use_color: bool = True,
    csv_path: Path | None = None,
) -> AggregateMetrics:
    catalog  = _load_catalog(catalog_path)
    proposal = _load_proposal(proposal_path)

    gt_columns: dict[str, dict] = catalog.get("columns", {})
    gt_missing: list[dict]      = catalog.get("missing_standard_fields", [])

    # Build mapper lookup: source_column → (target_field, confidence)
    mapper_map: dict[str, tuple[str | None, float]] = {}
    for m in proposal.get("mappings", []):
        mapper_map[m["source_column"]] = (m.get("target_field"), m.get("confidence", 0.0))

    # Score each column in the ground truth catalog
    scores: list[ColumnScore] = []
    for col_name, gt_entry in gt_columns.items():
        mapper_target, mapper_conf = mapper_map.get(col_name, (None, 0.0))
        scores.append(_score_column(col_name, gt_entry, mapper_target, mapper_conf))

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

    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a MappingProposal JSON against the PAS-L ground truth catalog."
    )
    parser.add_argument("proposal", help="MappingProposal JSON file (output of schema_inference map)")
    parser.add_argument(
        "--catalog", default=str(CATALOG_PATH),
        help=f"Path to schema catalog YAML (default: {CATALOG_PATH})",
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

    args = parser.parse_args()
    proposal_path = Path(args.proposal)
    if not proposal_path.exists():
        sys.exit(f"Error: file not found: {proposal_path}")

    catalog_path = Path(args.catalog)
    if not catalog_path.exists():
        sys.exit(f"Error: catalog not found: {catalog_path}")

    csv_path = Path(args.csv_path) if args.csv_path else None

    metrics = score(
        proposal_path=proposal_path,
        catalog_path=catalog_path,
        quiet=args.quiet,
        show_all=args.show_all,
        use_color=not args.no_color,
        csv_path=csv_path,
    )

    if args.fail_below is not None and metrics.f1 < args.fail_below:
        print(f"FAIL: F1={metrics.f1:.4f} is below threshold {args.fail_below}")
        sys.exit(1)


if __name__ == "__main__":
    main()
