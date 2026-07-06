"""Human Review CLI — interactive terminal review of a MappingProposal.

Requires an interactive TTY. Raises SystemExit when run non-interactively.

Review tiers:
  ≥ 0.85  Auto-approved — summary table, no prompts.
  0.50–0.84  Flagged — [A]ccept / [M]odify / [S]kip per column.
  < 0.50   Low-confidence — same prompts, LLM rationale shown.

Outputs MappingDefinition JSON to schema_inference/mappings/.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .canonical.policy import CANONICAL_NAMES
from .models import (
    ApprovedMapping,
    ColumnMapping,
    MappingDefinition,
    MappingProposal,
    MissingFieldResolution,
)

MAPPINGS_DIR = Path(__file__).parent / "mappings"

console = Console()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _fmt_confidence(score: float) -> str:
    pct = f"{score:.0%}"
    if score >= 0.85:
        return f"[green]{pct}[/green]"
    if score >= 0.50:
        return f"[yellow]{pct}[/yellow]"
    return f"[red]{pct}[/red]"


def _get_reviewer_identity() -> str:
    try:
        name = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        email = subprocess.run(
            ["git", "config", "user.email"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        if name or email:
            return f"{name} <{email}>".strip(" <>")
    except Exception:
        pass
    env_reviewer = __import__("os").environ.get("SCHEMA_INFERENCE_REVIEWER")
    if env_reviewer:
        return env_reviewer
    try:
        return __import__("os").getlogin()
    except Exception:
        return "unknown"


def _display_column_panel(m: ColumnMapping, tier_label: str) -> None:
    dist_str = (
        "  ".join(f"{k}={v}" for k, v in list(m.__dict__.items())[:0])
        or "(none recorded)"
    )
    target_str = m.target_field or "[dim]→ extended_attributes[/dim]"
    method_str = f"[cyan]{m.method}[/cyan]"
    conf_str = _fmt_confidence(m.confidence)

    body = (
        f"[bold]Source:[/bold]  {m.source_column}  [dim](table: {m.source_table})[/dim]\n"
        f"[bold]Tier:[/bold]    {tier_label}\n"
        f"\n"
        f"[bold]Proposed:[/bold]  {target_str}  ({conf_str})\n"
        f"[bold]Method:[/bold]    {method_str}\n"
        f"[bold]SQL:[/bold]       [yellow]{m.sql_expression}[/yellow]\n"
        f"[bold]Notes:[/bold]     {m.notes}\n"
        f"  name_sim={m.name_similarity:.2f} | type_compat={m.type_compatibility:.2f} | pattern={m.pattern_bonus:.2f}"
    )
    console.print(Panel(body, title=f"[bold]{m.source_column}[/bold]", border_style="blue"))


def _prompt_action(m: ColumnMapping) -> tuple[ApprovedMapping, bool]:
    """Prompt [A]ccept / [M]odify / [S]kip. Returns (ApprovedMapping, added_to_extended)."""
    while True:
        choice = Prompt.ask(
            "[A]ccept / [M]odify / [S]kip (→ extended_attributes)",
            choices=["a", "m", "s", "A", "M", "S"],
            default="a",
        ).lower()

        if choice == "a":
            return (
                ApprovedMapping(
                    source_column=m.source_column,
                    source_table=m.source_table,
                    target_field=m.target_field,
                    sql_expression=m.sql_expression,
                    confidence=m.confidence,
                    method=m.method,
                    notes=m.notes,
                    reviewer_action="accepted",
                ),
                False,
            )

        if choice == "m":
            canonical_list = sorted(CANONICAL_NAMES)
            new_target_raw = Prompt.ask(
                f"Target field (blank = extended_attributes, options: {', '.join(canonical_list[:8])} ...)",
                default=m.target_field or "",
            ).strip()
            new_target: str | None = new_target_raw or None
            if new_target and new_target not in CANONICAL_NAMES:
                console.print(
                    f"[red]Unknown field '{new_target}'. "
                    f"Valid names: {', '.join(sorted(CANONICAL_NAMES))}[/red]"
                )
                continue
            new_sql = Prompt.ask(
                "SQL expression",
                default=m.sql_expression,
            ).strip()
            new_notes = Prompt.ask("Notes (optional)", default=m.notes).strip()
            return (
                ApprovedMapping(
                    source_column=m.source_column,
                    source_table=m.source_table,
                    target_field=new_target,
                    sql_expression=new_sql or m.sql_expression,
                    confidence=m.confidence,
                    method="manual",
                    notes=new_notes,
                    reviewer_action="modified",
                ),
                new_target is None,
            )

        if choice == "s":
            return (
                ApprovedMapping(
                    source_column=m.source_column,
                    source_table=m.source_table,
                    target_field=None,
                    sql_expression=m.source_column,
                    confidence=m.confidence,
                    method=m.method,
                    notes="Skipped by reviewer",
                    reviewer_action="skipped",
                ),
                True,
            )


# ─── Review phases ────────────────────────────────────────────────────────────

def _phase_auto_approved(
    mappings: list[ColumnMapping],
) -> list[ApprovedMapping]:
    if not mappings:
        return []

    table = Table(box=box.ROUNDED, header_style="bold green", show_lines=False)
    table.add_column("Source Column", style="dim")
    table.add_column("Target Field", style="green")
    table.add_column("Confidence", justify="right")
    table.add_column("SQL Expression", style="yellow", max_width=50)
    table.add_column("Method")

    for m in mappings:
        table.add_row(
            m.source_column,
            m.target_field or "extended_attributes",
            _fmt_confidence(m.confidence),
            m.sql_expression,
            m.method,
        )

    console.print("\n[bold green]Auto-approved (confidence ≥ 0.85):[/bold green]")
    console.print(table)
    console.print(
        f"[green]✓ {len(mappings)} column(s) auto-approved. No action required.[/green]"
    )

    return [
        ApprovedMapping(
            source_column=m.source_column,
            source_table=m.source_table,
            target_field=m.target_field,
            sql_expression=m.sql_expression,
            confidence=m.confidence,
            method=m.method,
            notes=m.notes,
            reviewer_action="auto_approved",
        )
        for m in mappings
    ]


def _phase_review(
    mappings: list[ColumnMapping], tier_label: str
) -> tuple[list[ApprovedMapping], list[str]]:
    """Returns (approved_list, extra_extended_attrs)."""
    if not mappings:
        return [], []

    console.print(f"\n[bold yellow]{tier_label}:[/bold yellow] {len(mappings)} column(s) to review")
    approved: list[ApprovedMapping] = []
    extended_extra: list[str] = []

    for m in mappings:
        _display_column_panel(m, tier_label)
        am, to_extended = _prompt_action(m)
        approved.append(am)
        if to_extended:
            extended_extra.append(m.source_column)

    return approved, extended_extra


def _phase_missing_fields(
    missing: list[str],
) -> list[MissingFieldResolution]:
    if not missing:
        return []

    console.print(f"\n[bold yellow]Missing standard fields:[/bold yellow] {len(missing)} field(s) have no source match")
    resolutions: list[MissingFieldResolution] = []

    for field_name in missing:
        console.print(f"\n  [bold]{field_name}[/bold] — no source column mapped to this field")
        choice = Prompt.ask(
            "  [1] NULL  [2] Hardcode a value  [3] SQL derivation",
            choices=["1", "2", "3"],
            default="1",
        )
        if choice == "1":
            resolutions.append(
                MissingFieldResolution(
                    target_field=field_name,
                    resolution="NULL",
                )
            )
        elif choice == "2":
            val = Prompt.ask(f"  Hardcoded value for {field_name}").strip()
            resolutions.append(
                MissingFieldResolution(
                    target_field=field_name,
                    resolution="HARDCODED",
                    hardcoded_value=val,
                )
            )
        else:
            sql = Prompt.ask(f"  SQL expression for {field_name}").strip()
            resolutions.append(
                MissingFieldResolution(
                    target_field=field_name,
                    resolution="DERIVED",
                    derivation_sql=sql,
                )
            )

    return resolutions

def _phase_contested_mappings(
    contested: list[dict],
    approved: list[ApprovedMapping],
) -> None:
    """MAP-3: dedicated review phase for near-tie contests the pipeline couldn't
    resolve. For each contest, show the competing columns and let the reviewer
    pick the winner (or send all to extended_attributes). Mutates `approved` in
    place — the chosen winner keeps the target, the rest are set to unmapped."""
    if not contested:
        return

    console.print(
        f"\n[bold yellow]Contested mappings:[/bold yellow] "
        f"{len(contested)} target(s) had near-tied source columns the agents couldn't resolve"
    )

    approved_by_col = {a.source_column: a for a in approved}

    for contest in contested:
        target = contest["target_field"]
        competing = contest["competing_columns"]
        confidences = contest.get("confidences", {})

        console.print(f"\n  [bold]{target}[/bold] — {len(competing)} columns competing:")
        for i, col in enumerate(competing, 1):
            conf = confidences.get(col)
            conf_str = f"(confidence {conf:.2f})" if conf is not None else ""
            console.print(f"    [{i}] {col} {conf_str}")

        options = [str(i) for i in range(1, len(competing) + 1)] + ["x"]
        choice = Prompt.ask(
            f"  Which column maps to {target}?  "
            f"[1-{len(competing)}] pick a column  [x] none (all → extended_attributes)",
            choices=options,
            default="1",
        )

        if choice == "x":
            winner = None
        else:
            winner = competing[int(choice) - 1]

        for col in competing:
            am = approved_by_col.get(col)
            if am is None:
                continue
            if col == winner:
                am.target_field = target
                am.reviewer_action = "modified"
                am.notes = (am.notes + " | " if am.notes else "") + f"[reviewer chose as {target} winner]"
            else:
                am.target_field = None
                am.reviewer_action = "modified"
                am.notes = (am.notes + " | " if am.notes else "") + f"[reviewer: not {target}]"

def _phase_extended_attrs(
    extended_columns: list[str],
) -> list[str]:
    """Confirm or individually review columns routed to extended_attributes."""
    if not extended_columns:
        return []

    console.print(
        f"\n[bold]extended_attributes routing:[/bold] "
        f"{len(extended_columns)} column(s) will be included in the JSON blob"
    )

    table = Table(box=box.SIMPLE, show_header=False)
    table.add_column("Column", style="dim")
    for c in extended_columns:
        table.add_row(c)
    console.print(table)

    choice = Prompt.ask(
        "[C]onfirm all / [R]eview individually",
        choices=["c", "r", "C", "R"],
        default="c",
    ).lower()

    if choice == "c":
        return extended_columns

    # Individual review
    kept: list[str] = []
    for col in extended_columns:
        assign = Prompt.ask(
            f"  '{col}' → [E]xtended_attributes / [M]ap to canonical field",
            choices=["e", "m", "E", "M"],
            default="e",
        ).lower()
        if assign == "e":
            kept.append(col)
        else:
            target = Prompt.ask(f"  Target field for '{col}'").strip()
            if target in CANONICAL_NAMES:
                console.print(
                    f"  [yellow]Note: mapping '{col}' → '{target}' recorded in notes only. "
                    f"Update approved_mappings manually if needed.[/yellow]"
                )
            else:
                kept.append(col)

    return kept


# ─── Metamodel wiring (MAP-1) ──────────────────────────────────────────────────

def _record_review_to_metamodel(
    proposal: MappingProposal,
    approved: list[ApprovedMapping],
) -> None:
    """Best-effort: persist reviewer actions to the metamodel store.

    Human review outcomes (accepted/modified/skipped, and which mappings were
    auto-approved without a prompt) are the feedback signal MAP-4's few-shot
    bank depends on. Uses proposal.run_id to update the matching
    mapping_history rows the orchestrator already wrote (agent pipeline
    runs); falls back to inserting fresh rows under a synthetic run_id when
    reviewing a legacy proposal that has no run_id (rule+LLM path, or a JSON
    file loaded from disk after the fact).

    Never raises — history is optional and must not block the review CLI.
    """
    try:
        from .metamodel.store import open_store
    except ImportError:
        return

    store = open_store()
    if not store:
        return
    try:
        fallback_run_id = proposal.run_id or f"review-{uuid.uuid4()}"
        for a in approved:
            updated = 0
            if proposal.run_id:
                updated = store.update_mapping_review(
                    proposal.run_id, a.source_column, a.reviewer_action
                )
            if not updated:
                store.record_mapping(
                    run_id=fallback_run_id,
                    source_name=proposal.source_name,
                    table_name=proposal.table_name,
                    source_column=a.source_column,
                    target_field=a.target_field,
                    confidence=a.confidence,
                    method=a.method,
                    sql_expression=a.sql_expression,
                    reviewer_action=a.reviewer_action,
                    notes=a.notes,
                )
    finally:
        store.close()


# ─── Non-interactive review (test-fixture generation, NOT real review) ───────

def auto_review_proposal(
    proposal: MappingProposal,
    accept_threshold: float = 0.70,
    output_path: str | Path | None = None,
    reviewer_identity: str = "auto-reviewer (non-interactive)",
) -> MappingDefinition:
    """Non-interactive stand-in for review_proposal() — for test-fixture
    generation, demos, and CI. NOT a substitute for human review of real
    client mappings; do not wire this into any production review path.

    review_proposal() hard-requires an interactive TTY (raises SystemExit
    otherwise), which blocks generating reviewer_action volume without a
    human manually clicking through prompts every time. MAP-4 Layer 1's
    few-shot bank curation (tools/curate_few_shot_bank.py) needs that volume
    — its critic_override_accepted origin specifically requires
    reviewer_action populated in mapping_history, which only review_proposal()
    (or this function) ever writes.

    Policy — a confidence-threshold PROXY for human judgment, not a
    correctness guarantee:
        confidence >= accept_threshold  -> accepted
        confidence <  accept_threshold  -> skipped (-> extended_attributes)
    Missing required fields all resolve to NULL (no human to ask).

    Writes the same MappingDefinition shape and metamodel records as
    review_proposal() — downstream consumers (curate_few_shot_bank.py,
    backfill.py) see no structural difference. Only reviewer_identity marks
    it as automated.
    """
    approved: list[ApprovedMapping] = []
    extended: list[str] = list(proposal.unmapped_columns)

    for m in proposal.mappings:
        if m.confidence >= accept_threshold:
            approved.append(ApprovedMapping(
                source_column=m.source_column,
                source_table=m.source_table,
                target_field=m.target_field,
                sql_expression=m.sql_expression,
                confidence=m.confidence,
                method=m.method,
                notes=m.notes,
                reviewer_action="accepted",
            ))
        else:
            approved.append(ApprovedMapping(
                source_column=m.source_column,
                source_table=m.source_table,
                target_field=None,
                sql_expression=m.source_column,
                confidence=m.confidence,
                method=m.method,
                notes="Auto-skipped by non-interactive reviewer (below accept_threshold)",
                reviewer_action="skipped",
            ))
            extended.append(m.source_column)

    extended = list(dict.fromkeys(extended))  # dedupe, preserve order
    resolutions = [
        MissingFieldResolution(target_field=f, resolution="NULL")
        for f in proposal.missing_standard_fields
    ]

    _record_review_to_metamodel(proposal, approved)

    definition = MappingDefinition(
        source_name=proposal.source_name,
        table_name=proposal.table_name,
        approved_mappings=approved,
        extended_attributes=extended,
        missing_field_resolutions=resolutions,
        reviewer_identity=reviewer_identity,
        reviewed_at=datetime.now(),
        profile_hash="",
    )

    if output_path is None:
        MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{proposal.source_name}_{proposal.table_name}_mapping.json"
        output_path = MAPPINGS_DIR / fname
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(definition.model_dump_json(indent=2), encoding="utf-8")

    return definition


# ─── Public API ───────────────────────────────────────────────────────────────

def review_proposal(
    proposal: MappingProposal,
    output_path: str | Path | None = None,
) -> MappingDefinition:
    """Interactively review a MappingProposal and return an approved MappingDefinition.

    Writes the definition as JSON to output_path (defaults to
    schema_inference/mappings/{source_name}_{table_name}_mapping.json).

    Raises SystemExit if not running in an interactive TTY.
    """
    if not sys.stdout.isatty():
        raise SystemExit(
            "review_proposal requires an interactive terminal. "
            "Run from a terminal, not in a pipe or CI job."
        )

    console.rule(f"[bold]Schema Review — {proposal.source_name} / {proposal.table_name}[/bold]")

    # Split by tier
    auto = [m for m in proposal.mappings if m.confidence >= 0.85]
    flagged = [m for m in proposal.mappings if 0.50 <= m.confidence < 0.85]
    low = [m for m in proposal.mappings if m.confidence < 0.50]

    approved: list[ApprovedMapping] = []
    extended: list[str] = list(proposal.unmapped_columns)

    # Phase 1: auto-approved
    approved.extend(_phase_auto_approved(auto))

    # Phase 2: flagged (0.50–0.84)
    ap2, ex2 = _phase_review(flagged, "Flagged for review (confidence 0.50–0.84)")
    approved.extend(ap2)
    extended.extend(ex2)

    # Phase 3: low-confidence (< 0.50)
    ap3, ex3 = _phase_review(low, "Low-confidence (< 0.50, LLM-assisted)")
    approved.extend(ap3)
    extended.extend(ex3)

    # Phase 4: missing required fields
    resolutions = _phase_missing_fields(proposal.missing_standard_fields)
    # MAP-3: dedicated review phase for unresolved near-tie contests
    _phase_contested_mappings(proposal.contested_mappings, approved)

    # Phase 5: extended_attributes confirmation
    extended = _phase_extended_attrs(list(dict.fromkeys(extended)))  # deduplicate, preserve order

    # MAP-1: persist reviewer actions (feedback signal for MAP-4's few-shot bank)
    _record_review_to_metamodel(proposal, approved)

    # Show metadata columns excluded
    if proposal.excluded_metadata_columns:
        console.print(
            f"\n[dim]Excluded CDC metadata columns (not mapped): "
            f"{', '.join(proposal.excluded_metadata_columns)}[/dim]"
        )

    reviewer = _get_reviewer_identity()
    definition = MappingDefinition(
        source_name=proposal.source_name,
        table_name=proposal.table_name,
        approved_mappings=approved,
        extended_attributes=extended,
        missing_field_resolutions=resolutions,
        reviewer_identity=reviewer,
        reviewed_at=datetime.now(),
        profile_hash="",  # populated by caller if SchemaProfile is available
    )

    # Write output
    if output_path is None:
        MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"{proposal.source_name}_{proposal.table_name}_mapping.json"
        output_path = MAPPINGS_DIR / fname

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(definition.model_dump_json(indent=2), encoding="utf-8")

    console.rule("[bold green]Review complete[/bold green]")
    console.print(f"[green]✓ Saved:[/green] {out}")
    auto_count = len([a for a in approved if a.reviewer_action == "auto_approved"])
    manual_count = len(approved) - auto_count
    console.print(
        f"  {auto_count} auto-approved | {manual_count} manually reviewed | "
        f"{len(extended)} → extended_attributes"
    )

    return definition
