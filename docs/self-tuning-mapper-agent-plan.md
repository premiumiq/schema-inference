# Self-Tuning Mapper Agent — Design Plan

Companion to [`mapper-agent-roadmap.md`](mapper-agent-roadmap.md) (MAP-4).
This is the deep dive on the concept with the biggest expected audience
impact: an agent that tunes the mapper's own rules, thresholds, and prompts
against ground truth until it hits a measurable accuracy target — per
client, per source — instead of an engineer hand-tuning the pipeline once
and hoping it generalizes.

---

## 1. Framing: there is no literal gradient, and that's fine

The instinct is right that this should work like gradient descent: compute a
loss, find the direction that reduces it, take a step, repeat. The honest
caveat is that most of what we'd want to tune — agent system prompts,
few-shot examples, tool-use policy — sits *in front of* a frozen foundation
model. We are not fine-tuning Claude's weights. There's no `∂loss/∂prompt`
in the calculus sense.

But the pipeline isn't one undifferentiable black box — it's a stack with
different tunable layers, and **some of those layers are literally
numeric and literally optimizable with real gradients or grid search**. The
plan below is a layered tuning stack, ordered from "this is real numeric
optimization, cheap and safe" to "this is LLM-rewriting-its-own-prompt,
powerful but needs guardrails." Self-tuning should climb this stack one
layer at a time, never skip to the expensive/risky layer first.

| Layer | What's tuned | How the "gradient" works | Cost | Risk |
|---|---|---|---|---|
| 0 | Rule-engine weights, thresholds, confidence floors | Real numeric optimization (grid/coordinate search or scipy) on a closed-form scoring function | Cheap, fast, deterministic | Low |
| 1 | Few-shot example bank | Retrieval-based curation — no LLM call to "tune," just better example selection | Cheap | Low |
| 2 | Agent system prompts / catalog notes | Textual critique → LLM proposes an edit → validated on holdout | Moderate (extra LLM calls + full pipeline re-runs) | Medium — needs guardrails (§5) |
| 3 | Critic/router trigger policy | Learned classifier replacing the hand-written `is_hard or below_floor` heuristic | Moderate, longer horizon | Medium |

---

## 2. The loss function (what we're minimizing)

Per [MAP-2](mapper-agent-roadmap.md#map-2-generalize-the-loss-function), the
loss is computed per column and rolled up to one scalar:

```
column_loss = w_target * target_error
            + w_calib  * (confidence - is_correct)^2          # Brier-style calibration
            + w_sql    * sql_incorrect                         # transformation correctness
            + w_hard   * is_hard_multiplier                     # weight hard cases higher

target_error    = 0 if verdict in (TP, TN) else 1
sql_incorrect   = 0 if verdict == TP and sql_runtime_matches_expected else 1 if target correct else 0
is_hard_multiplier = hard_weight if is_hard else 1.0

run_loss = mean(column_loss for all columns) + missing_field_penalty
```

This is the function every tuning layer below is trying to push down,
measured against a held-out ground-truth split (never the same split used
to diagnose/propose a change — see §5).

`score_mappings.py` already computes most of the raw ingredients (verdict,
confidence, is_hard, confidence_floor). MAP-2's job is mechanical: turn the
existing binary tallies into this weighted continuous sum and expose it as
one number per run, stored in `loss_runs` (MAP-1).

---

## 3. Layer 0 — Numeric knob tuning (the real gradient)

This is the part that's genuinely just optimization, no LLM involved in the
tuning step itself:

- **Rule-engine weights** — `_compute_confidence()` in `mapper.py`:
  `confidence = 0.65*name_sim + 0.25*type_compat + 0.10*pattern_bonus`.
  These three coefficients are free parameters. Given a labeled batch (the
  ground-truth catalog), grid search or `scipy.optimize.minimize` over
  `(α, β, γ)` with `α+β+γ=1` directly minimizes `run_loss`. This *is* gradient
  descent in the literal sense if you compute it with a smooth loss (the
  Brier term is smooth; swap the hard 0/1 target_error for a sigmoid-smoothed
  version during tuning only, so the whole thing is differentiable).
- **`llm_threshold`** (0.70 today) — the cutoff below which a column routes
  to the MappingAgent. Sweep this on the ground-truth set: too high routes
  cheap correct rule-mappings into the agent unnecessarily (cost, no
  accuracy gain); too low leaves genuinely ambiguous columns to the
  unreliable rule engine. One-dimensional sweep, trivial.
- **Per-field `confidence_floor`** in the schema catalog — these are
  currently hand-set per column by whoever wrote the catalog. Once we have
  enough labeled runs, the floor that minimizes false-confident-wrong-answers
  for that specific column can be fit directly from the calibration term.
- **Critic trigger thresholds** (`target_hard_columns`, `target_below_floor`)
  — these are booleans today; could generalize to "review if predicted
  P(error) > τ" once Layer 3 exists, but until then, τ on the existing
  below-floor margin is a free numeric parameter too.

**Implementation:** a `tools/tune_rule_weights.py` script, runnable in CI or
on demand: load ground truth, grid-search or `scipy.optimize`, write the
winning weights into `agent_config.yml` (new `rule_engine.weights` section),
re-score to confirm improvement, log before/after loss to `loss_runs`.

This layer alone is worth building first — it's the "self-tuning" that's
actually riskless and mechanically verifiable, and it'll absorb a chunk of
the accuracy gap before any prompt touches Claude.

---

## 4. Layer 1 — Few-shot example bank (retrieval, not rewriting)

Every confirmed TP on a hard column, and every CriticAgent override that
later survived human review, is a labeled example of "this is what correct
reasoning looks like for this kind of column." Persist these (MAP-1) keyed
by column-profile signature (name pattern, inferred type, is_cents_integer,
is_coded_column, etc.).

At MappingAgent/CriticAgent invocation time, retrieve the top-K most similar
past examples (cheap similarity: same flags + fuzzy name match, no embedding
model needed at this scale) and inject them into the prompt as worked
examples: "Here's a past case like this one and the correct reasoning."

**Why this counts as tuning, not just engineering:** which examples get
retrieved and how many changes the agent's effective behavior without
touching a single line of the system prompt — it's the LLM-native analogue
of "more training data near this point in input space," and it can be
evaluated the same loss-driven way: does adding examples X reduce loss on
held-out columns versus a baseline with no examples? If a candidate example
correlates with *worse* downstream loss when included (overfit to a quirky
one-off), retire it from the bank. The bank's contents are the tuned
parameter; curation is the update rule.

---

## 5. Layer 2 — Textual prompt tuning (the actual "gradient equivalent")

This is the layer the question is really asking about, and it's a known
pattern in the literature (automatic prompt engineering / OPRO-style
optimization-by-prompting) — worth naming honestly rather than presenting as
novel: the "gradient" is a **textual critique of failure cases**, and the
"update step" is an LLM rewriting the prompt guided by that critique. The
loop:

```
1. DIAGNOSE   — run the pipeline on a labeled training split; collect every
                 column with verdict in (FP, FN, WRONG_TARGET) plus any TP/TN
                 with a calibration penalty above some threshold (confident
                 but only barely right — fragile cases).
2. SUMMARIZE   — group failures by pattern (a "PromptDiagnosisAgent" call):
                 not "column X failed" but "the agent keeps treating *_CD
                 suffix columns as codes-to-pass-through even when the value
                 catalog flags them as needing translation" — i.e. one
                 textual failure mode that explains several individual misses.
3. PROPOSE     — a "PromptTunerAgent" proposes ONE targeted edit to the
                 system prompt or catalog notes addressing that failure mode
                 (e.g. add a rule, add a counter-example, sharpen an existing
                 instruction). One edit per round — not a rewrite — so the
                 effect of each change is attributable.
4. VALIDATE    — re-run the FULL pipeline against a HELD-OUT split (never
                 seen in steps 1-3) and recompute run_loss.
5. ACCEPT/ROLLBACK — if held-out loss improved AND no regression on any
                 individual column that was previously correct, commit the
                 prompt edit as a new `prompt_version` (MAP-1) with the
                 before/after loss recorded. Otherwise discard the edit and
                 try a different diagnosis/proposal next round.
6. REPEAT      — until loss plateaus or a round budget is exhausted.
```

**This is the closest practical analogue to gradient descent available
without fine-tuning weights**: the "direction" is the failure-mode
diagnosis, the "step" is one targeted prompt edit, the "step size control"
is the held-out validation gate, and "momentum/learning rate" is the
round budget plus "one edit at a time" discipline (prevents wild swings from
a single overcorrected prompt rewrite).

### Guardrails (non-negotiable, this is the risky layer)

- **Train/holdout split is mandatory and never crossed.** Steps 1-3 only see
  the training split. Step 4 only ever scores the holdout split. Without
  this, the loop will happily memorize the ground-truth catalog's specific
  44 columns and produce a prompt that's brittle on every other source.
- **One edit per round, append-only with version history.** Never let the
  tuner rewrite the whole prompt — diff-sized edits only, so a regression is
  a one-line revert, not "which of the twelve things I changed broke this."
- **Human approval gate before a tuned prompt reaches a real client run.**
  Layer 0/1 changes (numeric weights, example bank) can auto-deploy — they're
  mechanically verifiable and low-blast-radius. Layer 2 changes (prompt
  edits) should land in `prompt_versions` as *proposed*, with the
  before/after loss and the diagnosis attached, and require a human merge
  before becoming the active prompt for production mapping runs. This is
  the same posture as a model-version bump — show the diff and the eval
  delta, let a person say yes.
- **Round budget + early stopping.** Cap rounds (e.g. 10) and stop early if
  three consecutive rounds fail to beat the best-so-far holdout loss —
  otherwise this becomes an expensive, possibly never-converging loop given
  LLM-as-judge / LLM-as-proposer non-determinism.
- **Determinism check.** Because the proposer and the agents being tuned are
  both LLMs, re-running the SAME accepted prompt version against the SAME
  holdout split should be checked for run-to-run variance before trusting a
  single round's "improvement" — a tiny holdout set + temperature>0 can show
  noise that looks like signal. Either fix temperature low for eval runs or
  average over a few repeats before accepting a round.

---

## 6. Layer 3 — Learned critic trigger (longer horizon, optional)

Once enough rounds of Layer 0-2 have accumulated labeled (column profile →
correct/incorrect) pairs in the metamodel, the critic's hand-written
`is_hard or below_floor` trigger could be replaced by a small learned
classifier (logistic regression on profile features → predicted error
probability), with the threshold itself tuned the same Layer-0 way. This is
explicitly optional and lower priority — the hand-written heuristic is
already working reasonably (hard-F1 0.92 per PR #125's real-data run) and a
learned router only pays off once there's enough volume across multiple
clients/sources for the classifier to generalize instead of overfitting to
PAS-L specifically.

---

## 7. What "client-specific measurable accuracy" looks like in practice

The deliverable this whole stack is building toward, concretely:

1. Client sends a sample extract + a manually-mapped subset (their own SMEs
   map 20-30 columns by hand — this becomes that client's ground-truth catalog,
   same shape as `pasl_schema_catalog.yml`).
2. Run the pipeline once, cold, against that catalog → baseline loss/F1.
3. Run the Layer 0 tuner (numeric, fast, cheap) → report the loss delta.
4. If still short of an agreed accuracy bar, run a bounded Layer 2 session
   (e.g. 5 rounds) → report the loss delta and the specific prompt edits
   made, attached to the client's `prompt_version` history.
5. Hand the client a number: "starting accuracy 78% F1, post-tuning 94% F1,
   here are the three rule changes and two prompt edits that got us there."

That's a sales/demo artifact, not just an engineering improvement — it's the
"this isn't an off-the-shelf tool, it's the same accelerator continuously
calibrating itself to your specific PAS export" pitch.

---

## 8. Build status

| Layer | Status | File | Notes |
|-------|--------|------|-------|
| MAP-1 (metamodel) | **Done** | `schema_inference/metamodel/store.py` | SQLite, 4 tables, wired into orchestrator + reviewer + evaluator |
| MAP-2 (loss function) | **Done** | `scripts/score_mappings.py` | Source-generic, continuous loss, PAS-L + PAS-M value catalogs |
| Layer 0 (rule weights) | **Done** | `tools/tune_rule_weights.py` | PAS-L: 0.65/0.25/0.10 → 0.15/0.20/0.65, F1 100% |
| Layer 1 (few-shot bank) | **Done** | `tools/curate_few_shot_bank.py`, `schema_inference/metamodel/few_shot.py` | Bank empty in practice — needs production run volume |
| Layer 2 (prompt tuning) | **Done** | `tools/tune_prompts.py` | Mechanics verified with fake LLM client; live quality needs `ANTHROPIC_API_KEY` run |
| Layer 3 (learned trigger) | **Deferred** | — | Gate: needs cross-source `mapping_history` volume; PAS-M catalog exists but no runs yet |

**Open gap:** `agent_config.yml` has one global `rule_engine.weights` section.
Running Layer 0 `--apply` for PAS-M clobbers PAS-L's tuned weights. Per-source
weights section must be designed before independent tuning of multiple sources.

**Next step:** run Layer 2 live (with `ANTHROPIC_API_KEY`) against PAS-M using
its ground truth catalog — PAS-M has genuine headroom (88.2% F1, 66.7% hard-F1
ceiling from rule engine alone) and decontaminated prompts, making it the right
vehicle for a real end-to-end self-tuning demonstration.
