"""MAP-4 Layer 2 — textual prompt tuning loop (the "gradient equivalent").

There is no literal gradient through a frozen LLM. The analog implemented here,
per docs/self-tuning-mapper-agent-plan.md (Layer 2):

  1. DIAGNOSE  — score the current prompt on the TRAIN split; collect failures
                 (FP/FN/WRONG_TARGET) and fragile-but-correct columns (high
                 calibration penalty). Pure Python, no LLM call.
  2. SUMMARIZE — a PromptDiagnosisAgent (LLM) names the recurring failure
                 PATTERN behind the individual misses.
  3. PROPOSE   — a PromptTunerAgent (LLM) makes ONE targeted edit to the
                 prompt addressing that pattern. A diff-size guardrail
                 (difflib ratio) rejects no-op edits and full rewrites alike.
  4. VALIDATE  — score the candidate on the HOLDOUT split (never seen in
                 steps 1-3) and check for regressions on previously-correct
                 holdout columns.
  5. ACCEPT/REJECT — log every round to prompt_versions (accepted=False —
                 this loop NEVER sets accepted=True; only a human, via
                 --accept, performs the merge). Track the best-so-far
                 candidate across rounds.
  6. REPEAT    — up to --rounds, with early stop after 3 consecutive
                 non-improving rounds.

A determinism check (re-score the session's best candidate a few times) runs
at the end, since LLM sampling means a single round's "improvement" can be
noise.

Train/holdout split is fixed for the whole session (seeded, stratified by
is_hard) and never crossed — DIAGNOSE/PROPOSE only see train-split failures;
VALIDATE only scores holdout-split columns.

Usage:
    python tools/tune_prompts.py --agent mapping --rounds 5
    python tools/tune_prompts.py --agent critic --rounds 5 --source-name pasl
    python tools/tune_prompts.py --accept <version_id>
    python tools/tune_prompts.py --determinism-check <version_id> --repeats 3

Requires ANTHROPIC_API_KEY (the diagnose/propose/validate steps make real
LLM calls). diagnosis_client/tuner_client params on run_tuning_session() are
injectable for testing the loop mechanics without live calls.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import importlib
import io
import json
import sys
import tempfile
from pathlib import Path
from statistics import mean, pstdev

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

import yaml

from schema_inference.agents.orchestrator import run_mapping
from schema_inference.agents.throttle import call_with_retry
from schema_inference.mapper import _CDC_RE
from schema_inference.metamodel.store import open_store
from schema_inference.profiler import profile_file

import score_mappings as scorer

GROUND_TRUTH_DIR = Path(
    __import__("os").environ.get(
        "SCHEMA_INFERENCE_CATALOG_DIR",
        str(_REPO_ROOT / "examples" / "insurance" / "ground_truth"),
    )
)
DEFAULT_DATA_FILE = {
    "pasl": _REPO_ROOT / "examples" / "insurance" / "test_data" / "pasl_policy.dat",
    "pasm": _REPO_ROOT / "examples" / "insurance" / "test_data" / "pasm_policy.dat",
}

# Diff-size guardrail: candidate must differ from current (ratio <= MAX) but
# not be a near-total rewrite (ratio >= MIN). ratio is difflib's similarity
# score (1.0 = identical). A genuine single-rule addition to a realistic
# (hundreds-of-chars) prompt typically lands ~0.97-0.99 — MAX must sit above
# that band and only reject near-exact duplicates (ratio > 0.995, i.e. the
# model basically echoed the prompt back) or trivial whitespace reformatting.
MIN_DIFF_RATIO = 0.50
MAX_DIFF_RATIO = 0.995

DEFAULT_MAX_ROUNDS = 5
EARLY_STOP_AFTER = 3
CALIBRATION_FRAGILE_THRESHOLD = 0.15

AGENT_PROMPT_MODULE = {
    "mapping": "schema_inference.agents.mapping_agent",
    "critic": "schema_inference.agents.critic_agent",
}


def _current_prompt_text(agent_name: str) -> str:
    """What a fresh session starts tuning from: the active accepted prompt
    if one exists, else the agent module's hardcoded constant."""
    store = open_store()
    active = None
    if store:
        try:
            active = store.get_active_prompt(agent_name)
        finally:
            store.close()
    if active:
        return active
    mod = importlib.import_module(AGENT_PROMPT_MODULE[agent_name])
    return mod._SYSTEM_PROMPT


# ── Train/holdout split ───────────────────────────────────────────────────────

def train_holdout_split(
    catalog_columns: dict, seed: int = 42, train_frac: float = 0.7
) -> tuple[set[str], set[str]]:
    """Deterministic, stratified by is_hard so both splits get a proportional
    share of hard columns. Computed once per session — never re-drawn
    mid-session, or DIAGNOSE could end up peeking at what becomes holdout
    data in a later round."""
    import random
    rng = random.Random(seed)

    hard = sorted(c for c, meta in catalog_columns.items() if meta and meta.get("is_hard"))
    easy = sorted(c for c in catalog_columns if c not in hard)
    rng.shuffle(hard)
    rng.shuffle(easy)

    def _split(items: list[str]) -> tuple[list[str], list[str]]:
        n_train = round(len(items) * train_frac)
        return items[:n_train], items[n_train:]

    hard_train, hard_holdout = _split(hard)
    easy_train, easy_holdout = _split(easy)
    return set(hard_train) | set(easy_train), set(hard_holdout) | set(easy_holdout)


# ── Run + score helper ────────────────────────────────────────────────────────

def _scoped_table(table, columns_subset: set[str]):
    """Restrict a TableProfile to columns_subset (+ CDC columns, which never
    get mapped anyway but orchestrator expects to see and exclude them).

    Cost lever: without this, every _run_and_score call ran the live agent
    pipeline over ALL 46 PAS-L columns regardless of whether train (32) or
    holdout (14) was being scored — the split only changed what got scored
    afterward, not what got computed. Scoping the table to just the relevant
    split cuts pipeline cost roughly in proportion to split size.

    Tradeoff: mapper._deduplicate()'s cross-column target-contention logic
    (when two source columns compete for the same canonical field, the higher-
    confidence one wins, the other is demoted) now only sees competitors
    within this split. If two competing columns happened to land on opposite
    sides of the train/holdout split, the winner could differ from a full-table
    run. Rare on PAS-L's column set (few genuine multi-column contentions) —
    accepted for the cost savings. Revisit if a future source has heavier
    target contention.
    """
    cols = [c for c in table.columns if c.name in columns_subset or _CDC_RE.match(c.name)]
    return table.model_copy(update={"columns": cols})


def _run_and_score(
    data_file: Path,
    source_name: str,
    columns_subset: set[str],
    mapping_prompt: str | None = None,
    critic_prompt: str | None = None,
    label: str = "",
) -> tuple["scorer.AggregateMetrics", list]:
    """Profile + run the agent pipeline scoped to columns_subset, then score
    it. record_to_metamodel=False — a tuning trial against a not-yet-accepted
    candidate is not a real mapping decision and must not pollute
    mapping_history (MAP-4 Layer 1 scans it for few-shot candidates).

    score()'s quiet=True only skips the per-column table — it still always
    prints the full metrics block, which gets confusing fast across a
    multi-round session (which block was train, which was holdout, which
    round?). Suppressed here in favor of one labeled summary line."""
    profile = profile_file(data_file, source_name=source_name)
    table = _scoped_table(profile.tables[0], columns_subset)

    run = run_mapping(
        table, source_name=source_name, use_agent=True,
        mapping_system_prompt=mapping_prompt,
        critic_system_prompt=critic_prompt,
        record_to_metamodel=False,
    )

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    try:
        tmp.write(run.proposal.model_dump_json(indent=2))
        tmp.close()
        with contextlib.redirect_stdout(io.StringIO()):
            metrics, scores = scorer.score(
                proposal_path=Path(tmp.name), source_name=source_name,
                quiet=True, use_color=False,
                columns_subset=columns_subset, return_scores=True,
            )
        if label:
            print(f"  [{label}] {len(columns_subset)} columns | mean_loss={metrics.mean_loss:.4f} | f1={metrics.f1:.4f}")
        return metrics, scores
    finally:
        Path(tmp.name).unlink(missing_ok=True)


# ── Step 1: DIAGNOSE (pure Python, no LLM) ────────────────────────────────────

def diagnose(scores: list, calib_threshold: float = CALIBRATION_FRAGILE_THRESHOLD) -> list[dict]:
    """Failures (FP/FN/WRONG_TARGET) plus fragile TP/TN (correct but high
    calibration penalty). `scores` must already be train-split-only — caller
    scores with columns_subset=train_columns before calling this."""
    failures = []
    for s in scores:
        is_failure = s.verdict in ("FP", "FN", "WRONG_TARGET")
        is_fragile = s.correct and s.calibration_penalty >= calib_threshold
        if is_failure or is_fragile:
            failures.append({
                "source_column": s.column_name,
                "gt_target": s.gt_target,
                "mapper_target": s.mapper_target,
                "confidence": s.mapper_confidence,
                "verdict": s.verdict,
                "is_hard": s.is_hard,
                "fragile_but_correct": is_fragile and not is_failure,
            })
    return failures


# ── Step 2: SUMMARIZE (PromptDiagnosisAgent — LLM call) ───────────────────────

_DIAGNOSIS_SYSTEM_PROMPT = """You are auditing a column-mapping agent's failures on a \
training split (you cannot see the held-out evaluation columns). Your job is to find the \
PATTERN behind multiple individual misses, not list them one by one.

Respond with ONLY a JSON object:
{
  "failure_mode": "<one or two sentences naming the recurring pattern>",
  "affected_columns": ["<col1>", "<col2>"],
  "suggested_fix_direction": "<one sentence: what kind of prompt change would address this>"
}"""


def _extract_json(text: str) -> dict:
    if "```" in text:
        import re
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            text = fenced[-1]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


def summarize_failures(failures: list[dict], current_prompt: str, client=None) -> dict:
    """LLM call. `client` is injectable for testing — must expose
    .messages.create(**kwargs) -> response with .content[0].text (the
    anthropic SDK response shape)."""
    if not failures:
        return {"failure_mode": "No training-split failures.", "affected_columns": [], "suggested_fix_direction": ""}

    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    user_prompt = (
        "Current system prompt:\n---\n" + current_prompt + "\n---\n\n"
        "Failures on the training split:\n" + json.dumps(failures, indent=2)
    )
    response = call_with_retry(client, dict(
        model="claude-sonnet-4-6", max_tokens=1024,
        system=[{"type": "text", "text": _DIAGNOSIS_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    ))
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    try:
        return _extract_json(text)
    except Exception:
        return {"failure_mode": text[:500], "affected_columns": [], "suggested_fix_direction": ""}


# ── Step 3: PROPOSE (PromptTunerAgent — LLM call) ─────────────────────────────

_TUNER_SYSTEM_PROMPT = """You are editing the system prompt of an insurance column-mapping \
agent to fix ONE specific, named failure pattern. Make the SMALLEST targeted change that \
addresses it — add one rule, one counter-example, or sharpen one existing instruction. \
Do NOT rewrite the prompt. Do NOT remove existing instructions unless they directly \
contradict the fix. Preserve the prompt's overall structure and its required JSON answer \
format exactly as given.

Respond with ONLY a JSON object:
{
  "edited_prompt": "<the full prompt text with your one targeted edit applied>",
  "rationale": "<one or two sentences: what you changed and why>"
}"""


def propose_edit(current_prompt: str, failure_mode: str, client=None) -> dict | None:
    """Returns {"prompt", "rationale", "diff_ratio"} or None if the proposal
    fails the diff-size guardrail (MIN_DIFF_RATIO/MAX_DIFF_RATIO) — too small
    a change is a no-op, too large is a rewrite, not a targeted edit."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    user_prompt = (
        f"Failure pattern to fix:\n{failure_mode}\n\n"
        f"Current prompt:\n---\n{current_prompt}\n---"
    )
    response = call_with_retry(client, dict(
        model="claude-sonnet-4-6", max_tokens=4096,
        system=[{"type": "text", "text": _TUNER_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    ))
    text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
    try:
        data = _extract_json(text)
    except Exception:
        return None

    edited = data.get("edited_prompt", "")
    if not edited:
        return None

    ratio = difflib.SequenceMatcher(None, current_prompt, edited).ratio()
    if not (MIN_DIFF_RATIO <= ratio <= MAX_DIFF_RATIO):
        return None

    return {"prompt": edited, "rationale": data.get("rationale", ""), "diff_ratio": round(ratio, 4)}


# ── Determinism check ─────────────────────────────────────────────────────────

def check_determinism(
    data_file: Path, source_name: str, agent_name: str,
    candidate_prompt: str, holdout_columns: set[str], repeats: int = 3,
) -> dict:
    """Re-validates the same candidate `repeats` times against the same
    holdout split. LLM sampling means a single round's "improvement" can be
    noise — report mean/stdev so a human can judge before accepting."""
    kwarg_name = "mapping_prompt" if agent_name == "mapping" else "critic_prompt"
    losses = []
    for i in range(repeats):
        metrics, _ = _run_and_score(
            data_file, source_name, holdout_columns, **{kwarg_name: candidate_prompt},
            label=f"DETERMINISM {i + 1}/{repeats} | HOLDOUT",
        )
        losses.append(metrics.mean_loss)
    return {
        "repeats": repeats, "losses": losses,
        "mean": round(mean(losses), 4),
        "stdev": round(pstdev(losses), 4) if len(losses) > 1 else 0.0,
    }


# ── Main tuning loop ──────────────────────────────────────────────────────────

def run_tuning_session(
    agent_name: str = "mapping",
    source_name: str = "pasl",
    data_file: Path | None = None,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    early_stop_after: int = EARLY_STOP_AFTER,
    seed: int = 42,
    diagnosis_client=None,
    tuner_client=None,
) -> dict:
    data_file = Path(data_file) if data_file else DEFAULT_DATA_FILE.get(source_name)
    if not data_file or not data_file.exists():
        raise FileNotFoundError(f"No data file for source '{source_name}'. Pass data_file explicitly.")

    catalog_path = GROUND_TRUTH_DIR / f"{source_name}_schema_catalog.yml"
    with open(catalog_path, encoding="utf-8") as f:
        catalog = yaml.safe_load(f) or {}
    catalog_columns = catalog.get("columns", {})

    train_cols, holdout_cols = train_holdout_split(catalog_columns, seed=seed)
    print(f"Train split: {len(train_cols)} columns | Holdout split: {len(holdout_cols)} columns\n")

    kwarg_name = "mapping_prompt" if agent_name == "mapping" else "critic_prompt"
    current_prompt = _current_prompt_text(agent_name)

    baseline_metrics, baseline_holdout_scores = _run_and_score(
        data_file, source_name, holdout_cols, **{kwarg_name: current_prompt},
        label="BASELINE | HOLDOUT",
    )
    best_prompt = current_prompt
    best_loss = baseline_metrics.mean_loss
    best_holdout_scores = baseline_holdout_scores
    print()

    store = open_store()
    parent_version_id = None
    rounds_log: list[dict] = []
    non_improving_streak = 0

    for round_num in range(1, max_rounds + 1):
        print(f"-- Round {round_num}/{max_rounds} --")

        _, train_scores = _run_and_score(
            data_file, source_name, train_cols, **{kwarg_name: best_prompt},
            label=f"ROUND {round_num} | TRAIN (diagnose)",
        )
        failures = diagnose(train_scores)
        print(f"  train failures/fragile: {len(failures)}")

        if not failures:
            print("  no failures on train split — nothing to diagnose, stopping early.")
            break

        diagnosis = summarize_failures(failures, best_prompt, client=diagnosis_client)
        print(f"  failure_mode: {str(diagnosis.get('failure_mode', ''))[:120]}")

        candidate = propose_edit(best_prompt, diagnosis.get("failure_mode", ""), client=tuner_client)
        if candidate is None:
            print("  proposal rejected by diff-size guardrail (no-op or full rewrite) — skipping round.")
            non_improving_streak += 1
            if non_improving_streak >= early_stop_after:
                print(f"  {early_stop_after} non-improving rounds — early stop.")
                break
            continue

        holdout_metrics, holdout_scores = _run_and_score(
            data_file, source_name, holdout_cols, **{kwarg_name: candidate["prompt"]},
            label=f"ROUND {round_num} | HOLDOUT (validate)",
        )

        prev_correct = {s.column_name for s in best_holdout_scores if s.correct}
        now_correct = {s.column_name for s in holdout_scores if s.correct}
        regressed = sorted(prev_correct - now_correct)

        improved = holdout_metrics.mean_loss < best_loss and not regressed

        version_id = None
        if store:
            version_id = store.record_prompt_version(
                agent_name=agent_name, prompt_text=candidate["prompt"],
                parent_version_id=parent_version_id,
                loss_before=best_loss, loss_after=holdout_metrics.mean_loss,
                diagnosis=json.dumps({
                    "failure_mode": diagnosis.get("failure_mode", ""),
                    "rationale": candidate.get("rationale", ""),
                    "diff_ratio": candidate.get("diff_ratio"),
                    "regressed_columns": regressed,
                }),
                accepted=False,
            )

        print(f"  holdout mean_loss: {best_loss:.4f} -> {holdout_metrics.mean_loss:.4f}"
              f"  {'(IMPROVED)' if improved else '(rejected)'}"
              + (f"  regressed: {regressed}" if regressed else ""))
        print(f"  logged as prompt_version {version_id}")

        rounds_log.append({
            "round": round_num, "version_id": version_id,
            "loss_before": best_loss, "loss_after": holdout_metrics.mean_loss,
            "improved": improved, "regressed": regressed,
        })

        if improved:
            best_prompt = candidate["prompt"]
            best_loss = holdout_metrics.mean_loss
            best_holdout_scores = holdout_scores
            parent_version_id = version_id
            non_improving_streak = 0
        else:
            non_improving_streak += 1
            if non_improving_streak >= early_stop_after:
                print(f"  {early_stop_after} non-improving rounds — early stop.")
                break

    if store:
        store.close()

    print(f"\nSession complete. Baseline holdout loss {baseline_metrics.mean_loss:.4f} -> best {best_loss:.4f}")
    best_version_id = next((r["version_id"] for r in reversed(rounds_log) if r["improved"]), None)

    determinism = None
    if best_version_id and best_prompt != current_prompt:
        print("\nRunning determinism check on best candidate (3 repeats)...")
        determinism = check_determinism(data_file, source_name, agent_name, best_prompt, holdout_cols, repeats=3)
        print(f"  losses: {determinism['losses']}  mean={determinism['mean']}  stdev={determinism['stdev']}")
        if determinism["stdev"] > 0.05:
            print("  WARNING: high run-to-run variance — review carefully before accepting.")

    if best_version_id:
        print(f"\nTo deploy: python tools/tune_prompts.py --accept {best_version_id}")
    else:
        print("\nNo improving candidate found this session — nothing to accept.")

    return {
        "baseline_loss": baseline_metrics.mean_loss,
        "best_loss": best_loss,
        "best_version_id": best_version_id,
        "rounds": rounds_log,
        "determinism": determinism,
    }


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MAP-4 Layer 2: textual prompt tuning loop.")
    parser.add_argument("--agent", choices=["mapping", "critic"], default="mapping")
    parser.add_argument("--source-name", default="pasl")
    parser.add_argument("--data-file", default=None)
    parser.add_argument("--rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--accept", default=None, metavar="VERSION_ID",
                         help="Human-merge a candidate version into production")
    parser.add_argument("--determinism-check", default=None, metavar="VERSION_ID", dest="determinism_check")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    if args.accept:
        store = open_store()
        if not store:
            sys.exit("Error: could not open metamodel store")
        n = store.accept_prompt_version(args.accept)
        store.close()
        print(f"Accepted version {args.accept}." if n else "No active candidate with that id.")
        return

    if args.determinism_check:
        store = open_store()
        rows = store.get_prompt_versions(args.agent) if store else []
        if store:
            store.close()
        match = next((r for r in rows if r["version_id"] == args.determinism_check), None)
        if not match:
            sys.exit("Error: version_id not found for this agent.")
        data_file = Path(args.data_file) if args.data_file else DEFAULT_DATA_FILE.get(args.source_name)
        catalog_path = GROUND_TRUTH_DIR / f"{args.source_name}_schema_catalog.yml"
        with open(catalog_path, encoding="utf-8") as f:
            catalog = yaml.safe_load(f) or {}
        _, holdout_cols = train_holdout_split(catalog.get("columns", {}), seed=args.seed)
        result = check_determinism(
            data_file, args.source_name, args.agent, match["prompt_text"], holdout_cols, repeats=args.repeats
        )
        print(json.dumps(result, indent=2))
        return

    data_file = Path(args.data_file) if args.data_file else None
    run_tuning_session(
        agent_name=args.agent, source_name=args.source_name,
        data_file=data_file, max_rounds=args.rounds, seed=args.seed,
    )


if __name__ == "__main__":
    main()
