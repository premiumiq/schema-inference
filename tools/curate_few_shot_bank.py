"""MAP-4 Layer 1 — curate the few-shot example bank from mapping_history.

Scans the metamodel store (MAP-1) for two qualifying origins, per
docs/self-tuning-mapper-agent-plan.md (Layer 1):

  hard_tp                  — a catalog is_hard=true column the pipeline got
                              right (verdict == 'TP')
  critic_override_accepted — a CriticAgent override (method == 'critic') that
                              a human later accepted or that auto-approved

For each qualifying column, takes the MOST RECENT matching mapping_history
row, requires a profile_signature (skips rows recorded before that field
existed — pre-Layer-1 history), and inserts into few_shot_examples unless an
active example for that (source, column, origin) already exists.

Not part of the regular pipeline — run manually, periodically, as history
accumulates:
    python tools/curate_few_shot_bank.py --source-name pasl
    python tools/curate_few_shot_bank.py --source-name pasl --retire <example_id> --reason "..."
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import yaml

from schema_inference.metamodel.store import open_store

GROUND_TRUTH_DIR = Path(
    __import__("os").environ.get("SCHEMA_INFERENCE_CATALOG_DIR")
    or str(_REPO_ROOT / "examples" / "insurance" / "ground_truth")
)


def _load_hard_columns(source_name: str) -> set[str]:
    catalog_path = GROUND_TRUTH_DIR / f"{source_name}_schema_catalog.yml"
    if not catalog_path.exists():
        return set()
    with open(catalog_path, encoding="utf-8") as f:
        catalog = yaml.safe_load(f) or {}
    return {
        name for name, meta in catalog.get("columns", {}).items()
        if meta and meta.get("is_hard") is True
    }


def _latest_by_column(rows: list[dict]) -> dict[str, dict]:
    """Most recent row per source_column (rows are ORDER BY recorded_at DESC already)."""
    latest: dict[str, dict] = {}
    for r in rows:
        latest.setdefault(r["source_column"], r)
    return latest


def curate(source_name: str) -> dict[str, int]:
    store = open_store()
    if not store:
        raise RuntimeError("Could not open metamodel store")

    counts = {"hard_tp_inserted": 0, "critic_inserted": 0, "skipped_existing": 0, "skipped_no_signature": 0}
    try:
        hard_columns = _load_hard_columns(source_name)
        all_history = store.get_mapping_history(source_name, limit=100000)

        # ── hard_tp: is_hard column, verdict TP ───────────────────────────────
        tp_rows = [r for r in all_history if r["verdict"] == "TP" and r["source_column"] in hard_columns]
        for col, row in _latest_by_column(tp_rows).items():
            if store.has_few_shot_example(source_name, col, "hard_tp"):
                counts["skipped_existing"] += 1
                continue
            if not row.get("profile_signature_json"):
                counts["skipped_no_signature"] += 1
                continue
            reasoning = row.get("notes") or f"Verified correct mapping: {col} -> {row['target_field']}."
            store.add_few_shot_example(
                source_name=source_name,
                source_column=col,
                target_field=row["target_field"],
                sql_expression=row["sql_expression"],
                reasoning=reasoning,
                profile_signature=json.loads(row["profile_signature_json"]),
                origin="hard_tp",
            )
            counts["hard_tp_inserted"] += 1

        # ── critic_override_accepted ──────────────────────────────────────────
        critic_rows = [
            r for r in all_history
            if r["method"] == "critic" and r.get("reviewer_action") in ("accepted", "auto_approved")
        ]
        for col, row in _latest_by_column(critic_rows).items():
            if store.has_few_shot_example(source_name, col, "critic_override_accepted"):
                counts["skipped_existing"] += 1
                continue
            if not row.get("profile_signature_json"):
                counts["skipped_no_signature"] += 1
                continue
            reasoning = row.get("notes") or f"Critic-overridden mapping accepted by reviewer: {col} -> {row['target_field']}."
            store.add_few_shot_example(
                source_name=source_name,
                source_column=col,
                target_field=row["target_field"],
                sql_expression=row["sql_expression"],
                reasoning=reasoning,
                profile_signature=json.loads(row["profile_signature_json"]),
                origin="critic_override_accepted",
            )
            counts["critic_inserted"] += 1
    finally:
        store.close()

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="MAP-4 Layer 1: curate the few-shot example bank.")
    parser.add_argument("--source-name", default="pasl")
    parser.add_argument("--retire", default=None, metavar="EXAMPLE_ID", help="Retire one example by id instead of curating")
    parser.add_argument("--reason", default="", help="Reason for --retire")
    args = parser.parse_args()

    if args.retire:
        store = open_store()
        if not store:
            sys.exit("Error: could not open metamodel store")
        try:
            n = store.retire_few_shot_example(args.retire, args.reason)
        finally:
            store.close()
        print(f"Retired {n} example(s)." if n else "No active example with that id.")
        return

    counts = curate(args.source_name)
    print(f"Curation for source '{args.source_name}':")
    print(f"  hard_tp examples added:                  {counts['hard_tp_inserted']}")
    print(f"  critic_override_accepted examples added: {counts['critic_inserted']}")
    print(f"  skipped (already in bank):               {counts['skipped_existing']}")
    print(f"  skipped (no profile signature recorded): {counts['skipped_no_signature']}")


if __name__ == "__main__":
    main()
