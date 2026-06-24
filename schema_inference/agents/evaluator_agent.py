"""EvaluatorAgent — scores a proposal against ground truth (demo/CI only).

A thin wrapper around scripts/score_mappings.py. It takes the final MappingProposal,
serializes it, runs the existing scorer, and returns the AggregateMetrics as a dict
(stored in AgentMappingRun.eval_score).

Only runs when eval_mode is enabled (it requires the ground-truth catalog, which
won't exist for a real unknown source).
"""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
from pathlib import Path

from ..models import MappingProposal

# Locate scripts/score_mappings.py at the repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SCORER_PATH = _REPO_ROOT / "scripts" / "score_mappings.py"


def _load_scorer():
    """Dynamically import scripts/score_mappings.py (it's not a package module)."""
    spec = importlib.util.spec_from_file_location("score_mappings", _SCORER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_evaluator(proposal: MappingProposal, run_id: str | None = None) -> dict | None:
    """Score the proposal against its source's ground truth. Returns metrics as a dict.

    Args:
        proposal: the final MappingProposal to score.
        run_id:   if given, forwarded to score_mappings.score() so verdicts
                  and the loss_runs row land in the metamodel store (MAP-1)
                  under the same run_id the orchestrator already recorded
                  mappings under.

    Returns None if the scorer or catalog is unavailable.
    """
    if not _SCORER_PATH.exists():
        return None

    scorer = _load_scorer()

    # Write the proposal to a temp JSON file for the scorer to read
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        tmp.write(proposal.model_dump_json(indent=2))
        tmp.close()

        metrics = scorer.score(
            proposal_path=Path(tmp.name),
            source_name=proposal.source_name,
            quiet=True,
            use_color=False,
            run_id=run_id,
        )
        # AggregateMetrics is a NamedTuple -> convert to dict
        return metrics._asdict()
    finally:
        os.unlink(tmp.name)