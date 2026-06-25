"""CLI entry point for the schema inference tool.

Usage:
    python -m schema_inference profile  <file> --source-name NAME [options]
    python -m schema_inference map      <profile_json> --table-name NAME [options]
    python -m schema_inference review   <proposal_json> [options]
    python -m schema_inference infer    <file> --source-name NAME [options]
    python -m schema_inference track    <file> --source-name NAME [options]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import VERSION


def _cmd_profile(args: argparse.Namespace) -> None:
    from .profiler import profile_file
    from .tracker import REGISTRY_DIR

    file_path = Path(args.file)
    if not file_path.exists():
        sys.exit(f"Error: file not found: {file_path}")

    source_name: str = args.source_name
    table_name: str | None = args.table_name

    print(f"Profiling {file_path.name} ...")
    profile = profile_file(
        file_path,
        source_name=source_name,
        table_name=table_name,
        delimiter=args.delimiter or None,
    )

    table = profile.tables[0]
    print(
        f"  {table.row_count:,} rows | {len(table.columns)} columns | "
        f"delimiter={table.delimiter!r}"
    )

    if args.output:
        out = Path(args.output)
    else:
        REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        source_dir = REGISTRY_DIR / source_name
        source_dir.mkdir(parents=True, exist_ok=True)
        out = source_dir / f"profile_{table.name}.json"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    print(f"Profile saved → {out}")


def _cmd_map(args: argparse.Namespace) -> None:
    from .models import SchemaProfile

    profile_path = Path(args.profile)
    if not profile_path.exists():
        sys.exit(f"Error: profile file not found: {profile_path}")

    profile = SchemaProfile.model_validate_json(profile_path.read_text(encoding="utf-8"))

    table_name: str = args.table_name
    table = next((t for t in profile.tables if t.name == table_name), None)
    if table is None:
        names = [t.name for t in profile.tables]
        sys.exit(f"Error: table '{table_name}' not in profile. Available: {names}")

    if args.agent:
        # ── Agent pipeline ───────────────────────────────────────────────
        from .agents.orchestrator import run_mapping

        print(f"Mapping {profile.source_name}/{table_name} with agent pipeline ...")
        run = run_mapping(
            table,
            source_name=profile.source_name,
            use_agent=True,
            concurrency=args.concurrency,
            eval_mode=args.eval,
        )
        proposal = run.proposal
        print(
            f"  rule={run.rule_pass_count} | agent={run.agent_pass_count} | "
            f"critic_overrides={run.critic_overrides} | {run.duration_seconds}s"
        )
        if run.eval_score:
            e = run.eval_score
            print(
                f"  ACCURACY: {e['correct']}/{e['total_columns']} correct | "
                f"F1={e['f1']:.2f} | hard-F1={e['hard_f1']:.2f}"
            )
    else:
        # ── Rule + single-batch LLM (original) ───────────────────────────
        from .mapper import map_table

        print(f"Mapping {profile.source_name}/{table_name} ...")
        proposal = map_table(
            table,
            source_name=profile.source_name,
            llm_threshold=args.threshold,
            use_llm=not args.no_llm,
        )

    mapped = sum(1 for m in proposal.mappings if m.target_field)
    unmapped = len(proposal.unmapped_columns)
    print(
        f"  {mapped} mapped | {unmapped} → extended_attributes | "
        f"{len(proposal.missing_standard_fields)} missing required fields"
    )

    serialized = proposal.model_dump_json(indent=2)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(serialized, encoding="utf-8")
        print(f"Proposal saved → {out}")
    else:
        print(serialized)


def _cmd_review(args: argparse.Namespace) -> None:
    from .models import MappingProposal

    proposal_path = Path(args.proposal)
    if not proposal_path.exists():
        sys.exit(f"Error: proposal file not found: {proposal_path}")

    proposal = MappingProposal.model_validate_json(
        proposal_path.read_text(encoding="utf-8")
    )
    output_path = Path(args.output) if args.output else None

    if args.auto:
        from .reviewer import auto_review_proposal
        definition = auto_review_proposal(
            proposal, accept_threshold=args.accept_threshold, output_path=output_path,
        )
        accepted = sum(1 for a in definition.approved_mappings if a.reviewer_action == "accepted")
        print(f"Auto-reviewed (non-interactive, threshold={args.accept_threshold}): "
              f"{accepted} accepted | {len(definition.approved_mappings) - accepted} skipped")
    else:
        from .reviewer import review_proposal
        review_proposal(proposal, output_path=output_path)


def _cmd_infer(args: argparse.Namespace) -> None:
    """Full pipeline: profile → map → review."""
    from .mapper import map_table
    from .models import SchemaProfile
    from .profiler import profile_file
    from .reviewer import review_proposal
    from .tracker import REGISTRY_DIR

    file_path = Path(args.file)
    if not file_path.exists():
        sys.exit(f"Error: file not found: {file_path}")

    source_name: str = args.source_name
    table_name: str | None = args.table_name

    # Step 1: Profile
    print(f"[1/3] Profiling {file_path.name} ...")
    profile = profile_file(
        file_path,
        source_name=source_name,
        table_name=table_name,
        delimiter=args.delimiter or None,
    )
    table = profile.tables[0]
    print(
        f"      {table.row_count:,} rows | {len(table.columns)} columns | "
        f"delimiter={table.delimiter!r}"
    )

    # Save profile
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    source_dir = REGISTRY_DIR / source_name
    source_dir.mkdir(parents=True, exist_ok=True)
    profile_path = source_dir / f"profile_{table.name}.json"
    profile_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    print(f"      Profile saved → {profile_path}")

    # Step 2: Map
    print(f"\n[2/3] Mapping columns ...")
    proposal = map_table(
        table,
        source_name=source_name,
        llm_threshold=args.threshold,
        use_llm=not args.no_llm,
    )
    mapped = sum(1 for m in proposal.mappings if m.target_field)
    print(
        f"      {mapped} columns mapped | "
        f"{len(proposal.unmapped_columns)} → extended_attributes"
    )

    # Step 3: Review
    print(f"\n[3/3] Starting interactive review ...")
    definition = review_proposal(proposal)
    # Patch profile_hash into the saved definition
    definition.profile_hash = profile.profile_hash
    out_path = (
        Path(__file__).parent
        / "mappings"
        / f"{source_name}_{table.name}_mapping.json"
    )
    out_path.write_text(definition.model_dump_json(indent=2), encoding="utf-8")


def _cmd_track(args: argparse.Namespace) -> None:
    from .profiler import profile_file
    from .tracker import BreakingSchemaChangeError, record_or_compare

    file_path = Path(args.file)
    if not file_path.exists():
        sys.exit(f"Error: file not found: {file_path}")

    source_name: str = args.source_name
    table_name: str | None = args.table_name

    print(f"Profiling {file_path.name} for schema tracking ...")
    profile = profile_file(
        file_path,
        source_name=source_name,
        table_name=table_name,
        delimiter=getattr(args, "delimiter", None),
    )
    table = profile.tables[0]

    try:
        sv, report = record_or_compare(
            table,
            source_name=source_name,
            force=args.force_accept_breaking,
        )
    except BreakingSchemaChangeError as exc:
        report = exc.report
        print(f"\n⚠️  BREAKING SCHEMA CHANGES DETECTED")
        for c in report.changes:
            if c.is_breaking:
                print(f"  {c.change_type.upper():15} {c.column_name}"
                      + (f"  ({c.old_value} → {c.new_value})" if c.old_value else ""))
        print(
            f"\nRun with --force-accept-breaking to record this version anyway.\n"
            f"New columns for mapping: {report.new_columns_for_mapping or 'none'}"
        )
        sys.exit(1)

    if report and not report.has_breaking_changes and report.changes:
        print(f"\nNon-breaking changes detected:")
        for c in report.changes:
            detail = f"  {c.change_type.upper():15} {c.column_name}"
            if c.change_type == "renamed":
                detail += f"  ({c.old_value} → {c.new_value}, sim={c.rename_similarity:.2f})"
            print(detail)
        if report.new_columns_for_mapping:
            print(f"\nNew columns — run 'infer' to map: {report.new_columns_for_mapping}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="schema_inference",
        description=f"PremiumIQ Schema Inference Tool v{VERSION}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── profile ──
    p_profile = sub.add_parser("profile", help="Profile a flat file → SchemaProfile JSON")
    p_profile.add_argument("file", help="Source .dat or .csv file")
    p_profile.add_argument("--source-name", required=True, help="Logical source name (e.g. pasl, broker_abc)")
    p_profile.add_argument("--table-name", default=None, help="Override table name (default: filename stem)")
    p_profile.add_argument("--delimiter", default=None, help="Column delimiter (auto-detected if omitted)")
    p_profile.add_argument("--output", default=None, help="Output JSON path (default: registry/{source}/profile_{table}.json)")

    # ── map ──
    p_map = sub.add_parser("map", help="Map a SchemaProfile → MappingProposal JSON")
    p_map.add_argument("profile", help="SchemaProfile JSON file")
    p_map.add_argument("--table-name", required=True, help="Table name within the profile")
    p_map.add_argument("--threshold", type=float, default=0.70, help="LLM trigger threshold (default: 0.70)")
    p_map.add_argument("--no-llm", action="store_true", help="Skip LLM pass entirely")
    p_map.add_argument("--output", default=None, help="Output JSON path (default: stdout)")
    p_map.add_argument("--agent", action="store_true", help="Use the 5-agent pipeline instead of single-batch LLM")
    p_map.add_argument("--concurrency", type=int, default=None, help="Agent concurrency (default: agent_config.yml's mapping_agent.concurrent_columns, or 10)")
    p_map.add_argument("--eval", action="store_true", help="Score the result against ground truth (demo/CI)")

    # ── review ──
    p_review = sub.add_parser("review", help="Interactively review a MappingProposal → MappingDefinition JSON")
    p_review.add_argument("proposal", help="MappingProposal JSON file")
    p_review.add_argument("--output", default=None, help="Output JSON path (default: mappings/{source}_{table}_mapping.json)")
    p_review.add_argument("--auto", action="store_true",
                           help="Non-interactive: accept/skip by confidence threshold instead of prompting. "
                                "Test-fixture generation only — not a substitute for real review.")
    p_review.add_argument("--accept-threshold", type=float, default=0.70, dest="accept_threshold",
                           help="With --auto: confidence at or above this is accepted, below is skipped (default: 0.70)")

    # ── infer (full pipeline) ──
    p_infer = sub.add_parser("infer", help="Full pipeline: profile → map → review")
    p_infer.add_argument("file", help="Source .dat or .csv file")
    p_infer.add_argument("--source-name", required=True, help="Logical source name")
    p_infer.add_argument("--table-name", default=None, help="Override table name")
    p_infer.add_argument("--delimiter", default=None, help="Column delimiter (auto-detected)")
    p_infer.add_argument("--threshold", type=float, default=0.70, help="LLM trigger threshold")
    p_infer.add_argument("--no-llm", action="store_true", help="Skip LLM pass")

    # ── track ──
    p_track = sub.add_parser("track", help="Track schema changes against stored version")
    p_track.add_argument("file", help="Source .dat or .csv file")
    p_track.add_argument("--source-name", required=True, help="Logical source name")
    p_track.add_argument("--table-name", default=None, help="Override table name")
    p_track.add_argument("--force-accept-breaking", action="store_true",
                         help="Record new version even when breaking changes exist")

    args = parser.parse_args()

    dispatch = {
        "profile": _cmd_profile,
        "map": _cmd_map,
        "review": _cmd_review,
        "infer": _cmd_infer,
        "track": _cmd_track,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
