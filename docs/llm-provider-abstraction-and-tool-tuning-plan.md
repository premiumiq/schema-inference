# LLM provider abstraction + tool/skill usage self-tuning (MAP-8, MAP-9)

Two related asks:

1. **MAP-8** — remove hardcoded Anthropic/Claude references so the mapping
   pipeline is LLM-provider-agnostic; Claude stays the default, but another
   deployment can point `agent_config.yml` at a different LLM/SLM.
2. **MAP-9** — today's self-tuning (Layers 0–2) tunes rule weights, few-shot
   examples, and system prompts. Nothing tunes *which tools the agent calls,
   when, or how well*. Add that as a new layer.

## Status

- **MAP-8 — implemented.** `schema_inference/llm/` (provider ABC, neutral
  types/errors, Anthropic + OpenAI adapters), the `agent_config.yml` `llm:`
  section, and all six call-site migrations described below are shipped
  (PR #3). Section retained as the design record for that work — read it as
  "what was built and why," not as a forward-looking plan.
- **MAP-9 — Steps 1–2 implemented, Step 3 scaffolded only.**
  `tool_usage_history` persistence and `tools/analyze_tool_usage.py`'s
  offline report are shipped (PR #4). The `mandatory_tool_triggers` config
  key and its loader exist but nothing reads them yet (explicit
  `TODO(MAP-9 follow-up)` at the call site), and the few-shot/prompt-loss
  integration described below is still just a plan — both need real
  `analyze_tool_usage.py` output from a scored live-agent run before they're
  worth implementing.

---

## Current state (why this is a real refactor, not a find/replace)

`anthropic.Anthropic()` is constructed directly, and model IDs are hardcoded
constants, in five places:

| File | What's hardcoded |
|---|---|
| `schema_inference/mapper.py:324-394` | `import anthropic`, `client = anthropic.Anthropic()`, `model="claude-haiku-4-5-20251001"` inline in `_run_llm_batch` |
| `schema_inference/agents/mapping_agent.py:28,289-291` | `MODEL = "claude-haiku-4-5-20251001"` module constant + client construction |
| `schema_inference/agents/critic_agent.py:28,176,304` | `MODEL = "claude-sonnet-4-6"` + two separate client constructions |
| `schema_inference/agents/sql_agent.py:28,79-107` | `MODEL = "claude-haiku-4-5-20251001"` + client construction |
| `schema_inference/agents/throttle.py:90-112` | `except anthropic.RateLimitError` — the *pacer itself* is provider-agnostic, but the exception type it catches isn't |
| `tools/tune_prompts.py:268-321` | two more client constructions, `model="claude-sonnet-4-6"` |

Plus `schema_inference/agents/tools.py` defines `TOOL_SCHEMAS` in Anthropic's
wire format (`input_schema`, not `parameters`), and `mapping_agent.py` sets
`cache_control` blocks that are meaningless to a non-Anthropic backend.

This matters for the plan: it's not just "swap the client," it's four things
that all currently assume Anthropic: **client construction, model-ID config,
tool-schema shape, and provider-specific exception types.**

---

## MAP-8: Provider abstraction

### New package: `schema_inference/llm/`

```
schema_inference/llm/
  __init__.py
  types.py       # LLMMessage, LLMToolDef, LLMToolCall, LLMResponse — provider-neutral
  errors.py      # LLMRateLimitError, LLMAuthError, LLMAPIError — normalized exceptions
  provider.py    # ABC: LLMProvider.complete(system, messages, tools=None, max_tokens=...) -> LLMResponse
  registry.py    # get_provider() -> reads agent_config.yml's `llm:` section, returns a cached instance
  providers/
    anthropic.py # wraps anthropic.Anthropic(); today's behavior, unchanged
    openai.py    # wraps the OpenAI SDK — also covers Azure OpenAI and any
                 # OpenAI-compatible server (Ollama, vLLM, LM Studio, etc.)
                 # via base_url, so one adapter buys most of the "SLM of choice" ask
```

`LLMToolDef` mirrors the JSON-Schema shape already used in `tools.py`
(`name`, `description`, `input_schema`) — that shape is *already*
provider-neutral JSON Schema, Anthropic's `input_schema` key is the only
wire-format wrinkle. Each provider adapter translates the neutral list to its
own wire shape (`input_schema` for Anthropic, `parameters` for
OpenAI-compatible) at the call boundary, so `tools.py` itself needs no
changes.

### Config: `agent_config.yml` gets an `llm:` section

```yaml
llm:
  provider: anthropic          # anthropic | openai  (extensible)
  providers:
    anthropic:
      api_key_env: ANTHROPIC_API_KEY
    openai:
      api_key_env: OPENAI_API_KEY
      base_url: null           # set for Azure OpenAI / Ollama / vLLM / etc.
  models:
    mapping_agent: claude-haiku-4-5-20251001
    critic_agent: claude-sonnet-4-6
    sql_agent: claude-haiku-4-5-20251001
    tune_prompts: claude-sonnet-4-6
```

Each agent's `MODEL = "..."` module constant becomes the *fallback default*
only, read through `load_agent_config()["llm"]["models"][...]` the same way
`_rule_weights()` and `_active_system_prompt()` already fall back to
hardcoded defaults when the config is missing or partial — matching the
repo's existing "config load must degrade gracefully" convention rather than
introducing a new one.

### Call-site changes

Every `import anthropic; client = anthropic.Anthropic(); client.messages.create(...)`
becomes `provider = get_provider(); provider.complete(...)`. `throttle.py`'s
`except anthropic.RateLimitError` becomes `except LLMRateLimitError` — the
pacer's logic (min spacing, backoff) doesn't change, only what it catches.

### What does *not* change

- The rule engine (`mapper.py`'s `_rule_map_column`, weighted name/type/pattern
  scoring) is pure Python — zero LLM dependency already, `--no-llm` keeps
  working unmodified.
- `MappingAgent`'s tool-use loop structure (`agents/tools.py`'s
  `TOOL_DISPATCH`, the per-column asyncio loop) is unchanged — only how it
  reaches the model changes.
- `metamodel/store.py`, `few_shot.py`, `tune_rule_weights.py` — no LLM calls,
  untouched.

### Open question to resolve with the user before implementing

Hand-roll two thin adapters (Anthropic + one OpenAI-compatible adapter that
covers OpenAI/Azure/Ollama/vLLM via `base_url`), vs. adopt a library like
`litellm` as the adapter layer itself. Recommendation: hand-roll. The repo's
own pattern (`agent_config.yml` + graceful-fallback loaders, no heavy
optional dependencies) fits a small home-grown `LLMProvider` ABC better than
pulling in a 100+-provider library whose tool-calling and prompt-caching
support varies unpredictably by provider. `litellm` can be added later as
*one more* implementation behind the same `LLMProvider` interface if the
provider matrix grows past what's worth hand-maintaining — the ABC boundary
is what makes that swap cheap either way.

### Sequencing (so this ships as a behavior-preserving refactor first)

1. Introduce `llm/` package + `AnthropicProvider` only. Every call site
   switches to `get_provider()`. No functional change — `provider: anthropic`
   is the only value that exists. Prove out with the existing test suite
   (`pytest tests/ -v -m "not snowflake"`) plus the CI smoke test.
2. Add the `llm:` config schema + `.env.example` / CLAUDE.md docs for the new
   knob, default still `anthropic` so nothing breaks for existing users/CI.
3. Add `OpenAIProvider` as the second implementation — this is the actual
   proof the abstraction holds up, and what unblocks "someone else picks a
   different model."
4. Update `docs/`, `CLAUDE.md` Config section, `bridge.py` (currently assumes
   `ANTHROPIC_API_KEY` is *the* env var that gates LLM availability).

---

## MAP-9: Tuning tool (and skill) usage, not just rules/prompts

### Where this sits relative to the existing layers

| Layer | Tunes | Mechanism | File |
|---|---|---|---|
| 0 | Rule-engine weights | Grid search vs. ground truth | `tools/tune_rule_weights.py` |
| 1 | Few-shot examples | Retrieval by profile-flag + name-sim | `metamodel/few_shot.py` |
| 2 | System prompts | Candidate generation + loss scoring, human-accepted | `tools/tune_prompts.py` |
| **3 (new)** | **Tool-call policy** | **Usage-vs-outcome analysis + two feedback paths** | new |

MappingAgent already has the raw material for this: `AgentTrace.tool_calls`
(`AgentToolCall`: `tool_name`, `inputs`, `output`) is produced for every
column, per `models.py:154-166`. It's just never persisted or analyzed —
`MetamodelStore.record_mapping` writes `mapping_history` (source_column,
target_field, confidence, method, verdict) but not the tool-call detail
behind it. That's the gap to close first.

### Step 1 — Instrumentation: persist tool-call traces, joinable to verdicts

Add a `tool_usage_history` table to `metamodel/store.py`:

```sql
CREATE TABLE IF NOT EXISTS tool_usage_history (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    source_column   TEXT NOT NULL,
    agent           TEXT NOT NULL,   -- 'mapping' | 'critic' | 'sql'
    tool_name       TEXT NOT NULL,
    call_order      INTEGER NOT NULL,
    inputs_json     TEXT NOT NULL,
    output_summary  TEXT,
    recorded_at     TEXT NOT NULL
);
```

`run_mapping()` in `orchestrator.py` already has every `AgentTrace` in hand
after the pipeline runs — write one row per `AgentToolCall`, keyed by
`run_id` + `source_column` so it can be joined against `mapping_history.verdict`
once `score_mappings.py` scores that run against ground truth. This mirrors
how `loss_runs` already links a run to a scored outcome — no new pattern,
same join key shape.

### Step 2 — Offline analysis: `tools/analyze_tool_usage.py` (Layer 3 script)

Same CLI shape as `tune_rule_weights.py` (`--source-name`, prints a report;
`--apply` writes recommendations back into config). What it computes, joining
`tool_usage_history` against `mapping_history.verdict`:

- **Per-tool marginal value** — for columns matched by profile signature
  (same flags `is_coded_column` / `is_cents_integer` / etc., same as
  `few_shot.py`'s retrieval key), compare accuracy when a given tool *was*
  called vs. wasn't. This is Layer 0's grid-search methodology applied to a
  categorical action space (tool subset) instead of continuous weights.
- **Under-triggering** — profile-flag combinations where skipping a specific
  tool correlates with a false-positive/false-negative spike (e.g. columns
  flagged `is_coded_column` that got mapped wrong without a
  `check_value_catalog` call).
- **Call efficiency** — % of columns hitting `max_tool_calls_per_column`
  (forced final answer, cut off mid-investigation — signal to raise the cap
  or improve tool ergonomics so fewer calls are needed), and duplicate
  same-tool-same-input calls (wasted round trips).
- **Unused findings** — a heuristic (substring/keyword match between a tool's
  output and the column's `reasoning_summary`) flagging tool calls whose
  result doesn't appear to have been used in the final answer. Imperfect but
  cheap, and useful as a smell signal rather than ground truth.

### Step 3 — Two feedback paths back into the running system

Don't invent a third tuning mechanism — route findings into the two that
already exist, plus one new deterministic gate that matches Layer 0's
"no-LLM, config-driven" philosophy:

1. **Deterministic mandatory-tool-trigger gating** (new, small, Layer-0-style).
   When analysis shows a profile-flag pattern has a strong, consistent
   correlation between skipping a tool and getting the mapping wrong, add a
   `mapping_agent.mandatory_tool_triggers` section to `agent_config.yml`
   (analogous to `rule_engine.weights_by_source`) that `mapping_agent.py`
   reads *before* the model's first turn: if the column's profile matches a
   listed pattern, auto-run that tool and inject its result into the prompt
   context, rather than leaving the decision to the model's discretion. This
   removes tool *selection* from LLM judgment exactly where the data says
   it's unambiguous — cheaper (no extra round trip needed to "convince" the
   model) and more reliable than prompt language alone.
2. **Fold into Layer 1 (few-shot retrieval)** — bias `few_shot.py`'s example
   selection toward examples whose stored trace includes the tool calls that
   analysis found to matter for that profile shape, so the model imitates
   good tool-usage patterns, not just good final answers. Requires storing a
   compact tool-call summary alongside each `few_shot_examples` row (small
   schema addition, same table).
3. **Fold into Layer 2 (prompt tuning)** — extend `tune_prompts.py`'s
   diagnosis/loss step so a candidate prompt is scored on tool-usage pattern
   too, not just final accuracy (e.g. "candidate B under-uses
   `check_value_catalog` on coded columns, causing N new false positives" is
   surfaced the same way a wording problem is today). This is the general
   case for patterns too fuzzy for the deterministic gate in (1).

### Skills

The repo has no literal "Skill" concept (Claude Agent Skills / progressive
disclosure files) today — the system prompt + fixed `TOOL_SCHEMAS` in
`tools.py` *is* the whole skill surface. Two implications:

- The tool-usage tuning above (Steps 1–3) is achievable entirely with what
  exists — no dependency on adopting literal Skills.
- If, after MAP-8, the team wants to package the PAS-L/PAS-M domain
  knowledge as retrievable Skills instead of a static system-prompt block
  (letting the agent load domain detail on demand rather than always paying
  for it in context), that's a bigger, separable change — and it would only
  make sense provider-side for Anthropic specifically (Skills are an
  Anthropic-API feature; other providers have no equivalent), which is in
  tension with MAP-8's goal. Flagging as an explicit **non-goal for now**,
  revisit only if a future need for progressive-disclosure prompting shows
  up independent of this plan.

### Sequencing

1. Add `tool_usage_history` table + write path from `orchestrator.py`
   (instrumentation only, no behavior change, low risk).
2. Ship `tools/analyze_tool_usage.py` producing a human-readable report
   (no auto-apply yet) — validates the signal is actually useful before
   wiring any feedback loop.
3. Add the deterministic `mandatory_tool_triggers` gate + config section,
   guarded the same way `tune_rule_weights.py --apply` is: a human reviews
   the before/after report, `--apply` writes the config.
4. Extend `few_shot.py` retrieval and `tune_prompts.py` diagnosis as
   described above.

---

## Suggested order of work

MAP-8 first — MAP-9's tool-usage analysis is provider-agnostic in principle
(it works on the persisted traces regardless of which LLM produced them),
but doing MAP-8 first means MAP-9's `analyze_tool_usage.py` is built once
against the final call-site shape instead of against Anthropic-specific
plumbing that then has to be revisited.
