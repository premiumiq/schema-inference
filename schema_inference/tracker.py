"""Schema Version Tracker — detects schema drift between recurring submissions.

Storage layout (under schema_inference/registry/{source_name}/):
  schema_v{N}.json          — SchemaVersion model
  changes_v{N}_to_v{N+1}.md — human-readable change report

Use case: a broker sends a monthly data file. The tracker hashes the column
fingerprint, compares it to the stored version, classifies changes, and blocks
on breaking changes (removed columns, type changes).

Breaking change handling:
  raise BreakingSchemaChangeError — caller prints the report and exits.
  Pass force=True to record the new version regardless (use with --force-accept-breaking).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from .models import (
    ColumnChange,
    ColumnFingerprint,
    SchemaChangeReport,
    SchemaVersion,
    TableProfile,
)

REGISTRY_DIR = Path(__file__).parent / "registry"
RENAME_THRESHOLD = 85  # rapidfuzz score (0–100) above which a remove+add is classified as rename


class BreakingSchemaChangeError(Exception):
    def __init__(self, report: SchemaChangeReport) -> None:
        super().__init__(
            f"Breaking schema changes detected in '{report.source_name}' "
            f"(v{report.from_version} → v{report.to_version}): "
            f"{sum(1 for c in report.changes if c.is_breaking)} breaking change(s)"
        )
        self.report = report


# ─── Fingerprint helpers ─────────────────────────────────────────────────────

def _build_fingerprints(table: TableProfile) -> list[ColumnFingerprint]:
    return [
        ColumnFingerprint(
            name=c.name,
            inferred_type=c.inferred_type,
            is_id_column=c.is_id_column,
            is_coded_column=c.is_coded_column,
            date_format=c.date_format,
        )
        for c in table.columns
    ]


def _hash_fingerprints(fingerprints: list[ColumnFingerprint]) -> str:
    sorted_fp = sorted(fingerprints, key=lambda f: f.name.lower())
    serialized = json.dumps(
        [fp.model_dump() for fp in sorted_fp], sort_keys=True
    )
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]


# ─── Registry I/O ────────────────────────────────────────────────────────────

def _source_dir(source_name: str) -> Path:
    d = REGISTRY_DIR / source_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _list_versions(source_name: str) -> list[int]:
    d = _source_dir(source_name)
    versions = []
    for f in d.glob("schema_v*.json"):
        try:
            n = int(f.stem.replace("schema_v", ""))
            versions.append(n)
        except ValueError:
            pass
    return sorted(versions)


def _load_version(source_name: str, version: int) -> SchemaVersion:
    path = _source_dir(source_name) / f"schema_v{version}.json"
    return SchemaVersion.model_validate_json(path.read_text(encoding="utf-8"))


def _save_version(version: SchemaVersion) -> Path:
    d = _source_dir(version.source_name)
    path = d / f"schema_v{version.version}.json"
    path.write_text(version.model_dump_json(indent=2), encoding="utf-8")
    return path


def _write_change_report(report: SchemaChangeReport) -> Path:
    d = _source_dir(report.source_name)
    fname = f"changes_v{report.from_version}_to_v{report.to_version}.md"
    path = d / fname

    lines = [
        f"# Schema Change Report — {report.source_name}",
        f"Version {report.from_version} → {report.to_version}",
        f"Generated: {datetime.now().isoformat()}",
        "",
    ]

    if report.has_breaking_changes:
        lines.append("## ⚠️  Breaking Changes")
        for c in report.changes:
            if c.is_breaking:
                lines.append(f"- **{c.change_type.upper()}** `{c.column_name}`"
                              + (f" (was: `{c.old_value}`, now: `{c.new_value}`)" if c.old_value else ""))
        lines.append("")

    non_breaking = [c for c in report.changes if not c.is_breaking]
    if non_breaking:
        lines.append("## Non-Breaking Changes")
        for c in non_breaking:
            detail = ""
            if c.change_type == "renamed":
                detail = f" (`{c.old_value}` → `{c.new_value}`, similarity={c.rename_similarity:.2f})"
            elif c.change_type == "added":
                detail = " (new column — routed to mapper)"
            lines.append(f"- **{c.change_type.upper()}** `{c.column_name}`{detail}")
        lines.append("")

    if report.new_columns_for_mapping:
        lines.append("## New Columns Requiring Mapping")
        for col in report.new_columns_for_mapping:
            lines.append(f"- `{col}`")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ─── Change detection ────────────────────────────────────────────────────────

def _detect_changes(
    old: SchemaVersion,
    new_fps: list[ColumnFingerprint],
    new_version: int,
) -> SchemaChangeReport:
    old_by_name = {f.name: f for f in old.columns}
    new_by_name = {f.name: f for f in new_fps}

    removed_names = set(old_by_name) - set(new_by_name)
    added_names = set(new_by_name) - set(old_by_name)

    changes: list[ColumnChange] = []

    # Rename detection: match removed → added by fuzzy name similarity
    rename_pairs: dict[str, str] = {}  # old_name → new_name
    for old_name in list(removed_names):
        for new_name in list(added_names):
            score = fuzz.ratio(old_name.lower(), new_name.lower())
            if score >= RENAME_THRESHOLD:
                rename_pairs[old_name] = new_name
                break

    # Renames — remove from removed/added sets
    for old_name, new_name in rename_pairs.items():
        removed_names.discard(old_name)
        added_names.discard(new_name)
        score = fuzz.ratio(old_name.lower(), new_name.lower())
        changes.append(
            ColumnChange(
                change_type="renamed",
                column_name=old_name,
                old_value=old_name,
                new_value=new_name,
                rename_similarity=round(score / 100, 3),
                is_breaking=False,
            )
        )

    # Removed — breaking
    for name in removed_names:
        changes.append(
            ColumnChange(
                change_type="removed",
                column_name=name,
                old_value=old_by_name[name].inferred_type,
                is_breaking=True,
            )
        )

    # Added — not breaking
    for name in added_names:
        changes.append(
            ColumnChange(
                change_type="added",
                column_name=name,
                new_value=new_by_name[name].inferred_type,
                is_breaking=False,
            )
        )

    # Type-changed — breaking
    for name in set(old_by_name) & set(new_by_name):
        if name in rename_pairs:
            continue
        old_fp = old_by_name[name]
        new_fp = new_by_name[name]
        if old_fp.inferred_type != new_fp.inferred_type:
            changes.append(
                ColumnChange(
                    change_type="type_changed",
                    column_name=name,
                    old_value=old_fp.inferred_type,
                    new_value=new_fp.inferred_type,
                    is_breaking=True,
                )
            )

    has_breaking = any(c.is_breaking for c in changes)
    new_for_mapping = [c.column_name for c in changes if c.change_type == "added"]

    return SchemaChangeReport(
        source_name=old.source_name,
        from_version=old.version,
        to_version=new_version,
        changes=changes,
        has_breaking_changes=has_breaking,
        new_columns_for_mapping=new_for_mapping,
    )


# ─── Public API ──────────────────────────────────────────────────────────────

def record_or_compare(
    table: TableProfile,
    source_name: str,
    force: bool = False,
    linked_mapping: str | None = None,
) -> tuple[SchemaVersion, SchemaChangeReport | None]:
    """Profile a table and either record it as v1 or compare against the latest version.

    Args:
        table:           TableProfile from the profiler.
        source_name:     Logical source system name.
        force:           If True, record new version even when breaking changes exist.
        linked_mapping:  Path to the approved MappingDefinition JSON for this version.

    Returns:
        (SchemaVersion, None) on first recording.
        (SchemaVersion, SchemaChangeReport) on comparison — may raise BreakingSchemaChangeError
            unless force=True.

    Raises:
        BreakingSchemaChangeError when breaking changes detected and force=False.
    """
    fps = _build_fingerprints(table)
    fp_hash = _hash_fingerprints(fps)

    existing_versions = _list_versions(source_name)

    if not existing_versions:
        # First time — record as v1
        sv = SchemaVersion(
            source_name=source_name,
            version=1,
            fingerprint_hash=fp_hash,
            columns=fps,
            recorded_at=datetime.now(),
            linked_mapping=linked_mapping,
        )
        path = _save_version(sv)
        print(f"[tracker] Recorded schema version 1 → {path}")
        return sv, None

    latest_version = existing_versions[-1]
    latest = _load_version(source_name, latest_version)

    # No change
    if latest.fingerprint_hash == fp_hash:
        print(f"[tracker] Schema unchanged (v{latest_version}, hash={fp_hash}). No update.")
        return latest, None

    # Change detected
    new_version = latest_version + 1
    report = _detect_changes(latest, fps, new_version)
    change_path = _write_change_report(report)
    print(f"[tracker] Change report → {change_path}")

    if report.has_breaking_changes and not force:
        raise BreakingSchemaChangeError(report)

    # Record new version
    sv = SchemaVersion(
        source_name=source_name,
        version=new_version,
        fingerprint_hash=fp_hash,
        columns=fps,
        recorded_at=datetime.now(),
        linked_mapping=linked_mapping,
    )
    path = _save_version(sv)
    print(f"[tracker] Recorded schema version {new_version} → {path}")
    return sv, report


def get_latest_version(source_name: str) -> SchemaVersion | None:
    """Return the latest stored SchemaVersion for a source, or None if never tracked."""
    versions = _list_versions(source_name)
    if not versions:
        return None
    return _load_version(source_name, versions[-1])
