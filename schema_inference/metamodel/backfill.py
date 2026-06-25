"""One-time backfill — import existing registry/mappings JSON into the metamodel (MAP-1).

Walks:
  schema_inference/registry/*/proposal_*.json   (MappingProposal — agent/rule output)
  schema_inference/mappings/*.json               (MappingDefinition — human-reviewed output)

Inserts each as mapping_history rows under a synthesized run_id derived from
the filename, with recorded_at taken from the file's mtime. Idempotent: a
file whose synthesized run_id already has rows in mapping_history is skipped
entirely on a re-run.

Not part of the regular pipeline — run manually, once, after MAP-1 lands:
    python -m schema_inference.metamodel.backfill
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .store import MetamodelStore, default_path, open_store

REGISTRY_DIR = Path(__file__).parent.parent / "registry"
MAPPINGS_DIR = Path(__file__).parent.parent / "mappings"


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def _backfill_proposal(store: MetamodelStore, path: Path) -> int:
    """One registry/{source}/proposal_*.json file (MappingProposal shape)."""
    run_id = f"backfill-{path.stem}"
    if store.has_run(run_id):
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    source_name = data.get("source_name", "")
    table_name = data.get("table_name", "")
    recorded_at = _mtime_iso(path)

    count = 0
    for m in data.get("mappings", []):
        row_id = store.record_mapping(
            run_id=run_id,
            source_name=source_name,
            table_name=table_name,
            source_column=m.get("source_column", ""),
            target_field=m.get("target_field"),
            confidence=m.get("confidence", 0.0),
            method=m.get("method", "rule"),
            sql_expression=m.get("sql_expression", ""),
            notes=m.get("notes", ""),
        )
        # record_mapping stamps recorded_at=now(); overwrite with the file's
        # mtime so backfilled history reflects when the run actually happened.
        store._conn.execute(
            "UPDATE mapping_history SET recorded_at = ? WHERE id = ?",
            (recorded_at, row_id),
        )
        count += 1
    store._conn.commit()
    return count


def _backfill_mapping_definition(store: MetamodelStore, path: Path) -> int:
    """One mappings/{source}_{table}_mapping.json file (MappingDefinition shape)."""
    run_id = f"backfill-{path.stem}"
    if store.has_run(run_id):
        return 0

    data = json.loads(path.read_text(encoding="utf-8"))
    source_name = data.get("source_name", "")
    table_name = data.get("table_name", "")
    recorded_at = _mtime_iso(path)

    count = 0
    for a in data.get("approved_mappings", []):
        row_id = store.record_mapping(
            run_id=run_id,
            source_name=source_name,
            table_name=table_name,
            source_column=a.get("source_column", ""),
            target_field=a.get("target_field"),
            confidence=a.get("confidence", 0.0),
            method=a.get("method", "rule"),
            sql_expression=a.get("sql_expression", ""),
            reviewer_action=a.get("reviewer_action"),
            notes=a.get("notes", ""),
        )
        store._conn.execute(
            "UPDATE mapping_history SET recorded_at = ? WHERE id = ?",
            (recorded_at, row_id),
        )
        count += 1
    store._conn.commit()
    return count


def run_backfill(db_path: Path | None = None) -> dict[str, int]:
    """Run the backfill. Returns {filename: rows_inserted} for files actually processed."""
    store = open_store(db_path)
    if not store:
        raise RuntimeError(f"Could not open metamodel store at {db_path or default_path()}")

    results: dict[str, int] = {}
    try:
        for path in sorted(REGISTRY_DIR.glob("*/proposal_*.json")):
            n = _backfill_proposal(store, path)
            if n:
                results[str(path)] = n

        if MAPPINGS_DIR.exists():
            for path in sorted(MAPPINGS_DIR.glob("*.json")):
                n = _backfill_mapping_definition(store, path)
                if n:
                    results[str(path)] = n
    finally:
        store.close()

    return results


def main() -> None:
    results = run_backfill()
    if not results:
        print("Nothing to backfill (no new files, or all already imported).")
        return
    total = sum(results.values())
    print(f"Backfilled {total} mapping_history row(s) from {len(results)} file(s):")
    for path, n in results.items():
        print(f"  {n:>4}  {path}")


if __name__ == "__main__":
    main()
