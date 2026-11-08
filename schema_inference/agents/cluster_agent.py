"""Agent pass over unclustered columns (MAP-10, step 2).

The deterministic clusterer in cluster.py reaches a ceiling that is semantic,
not a matter of tuning. Measured on PAS-L x PAS-M it hits F1 0.783 / precision
0.900, and every remaining miss needs domain knowledge that string similarity
cannot supply at any threshold:

    ANNU_PREM_AMT  <->  written_premium     "annual premium" and "written
                                            premium" are the same money in P&C
    COV_A_DED      <->  policy_deductible   Coverage A is the dwelling coverage

A token-coverage penalty was tried against these and rejected - it fixed one
pair and cost three others (see the MAP-10 PR). So this is where the pipeline
does what it does everywhere else: cheap deterministic logic first, reasoning
only for what the rules genuinely cannot reach.

Design notes:

  One call, not N^2.  ~35 leftover columns on one side and ~15 on the other is
                      500+ pairwise calls. Both lists go in a single request
                      instead, which is cheaper and lets the model see that
                      claiming written_premium for one column rules it out for
                      the rest.

  Abstention wins.    Most leftover columns genuinely have no counterpart - they
                      are concepts one system has and the other does not. A model
                      asked to find pairs will find pairs, so the prompt makes an
                      empty answer explicitly correct.

  Data over names.    Sample values are included precisely because the name
                      signal already failed on these columns. That is why they
                      are leftovers.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from ..llm.registry import get_provider, model_for
from ..models import ColumnProfile
from .throttle import call_with_retry
from .tools import _CATALOG_DIR

MODEL = "claude-sonnet-4-6"

# Below this the agent's own stated confidence is not worth acting on. Matches
# it is unsure about are dropped rather than surfaced as clusters, consistent
# with the abstain-don't-guess posture everywhere else in the pipeline.
MIN_AGENT_CONFIDENCE = 0.6

_SYSTEM_PROMPT = """You are a senior insurance data engineer reconciling two \
source systems that hold the same kind of business data under different \
schemas.

You are given the columns from each system that a deterministic matcher could \
NOT pair up. It already caught everything with an obvious name or shared value \
vocabulary, so what remains are columns whose names do not resemble each other \
even when they mean the same thing - or columns that genuinely have no \
counterpart at all.

Your job is to identify pairs that represent the SAME underlying business \
concept, reasoning from what the data actually contains, not from the column \
names. Two columns match when a single canonical field could hold both.

Each column may carry a "catalog" block describing what that column actually \
holds in its source system. This is curated domain knowledge and it is the \
strongest evidence available - when two catalog descriptions refer to the same \
business concept, that is a match even if the column names look nothing alike. \
Read it before anything else.

Critical instructions:

- Most of these columns have NO counterpart. Returning few matches, or none, is \
the correct answer far more often than returning many. Do not reach.
- Each column may appear in AT MOST ONE pair.
- Do not pair a column with something merely adjacent or related. A deductible \
is not a limit. A code is not the name it refers to. A date the policy started \
is not the date it was terminated.
- Source metadata (CDC flags, load timestamps, row versions) describes the \
system, not the business entity. It never matches anything.
- Give each match a confidence between 0 and 1 reflecting how certain you are \
that a single canonical field could hold both columns.

Respond with ONLY a JSON object, no prose and no code fences:
{
  "matches": [
    {
      "a": "<column name from source A>",
      "b": "<column name from source B>",
      "confidence": 0.0,
      "reasoning": "<one sentence, citing what the data shows>"
    }
  ]
}
Return {"matches": []} if nothing genuinely corresponds."""


@dataclass
class AgentMatch:
    source_a: str
    column_a: str
    source_b: str
    column_b: str
    confidence: float
    agreement: str
    reasoning: str

    def as_pair(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return ((self.source_a, self.column_a), (self.source_b, self.column_b))


def _model() -> str:
    return model_for("cluster_agent") or MODEL


# ── Value catalog context ────────────────────────────────────────────────────
# The columns that reach this agent are exactly the ones whose NAMES were not
# enough - that is why the deterministic pass left them behind. Sending only a
# name, a type and six sample values asks the model to re-derive semantics that
# the project already has written down.
#
# The value catalogs carry that semantics. ANNU_PREM_AMT is described as
# "Annual written premium ... integer cents" and written_premium as "Annual
# written premium, ALREADY in decimal dollars" - the same phrase on both sides.
# MNTHLY_PREM_AMT is described as "Monthly premium = ANNU_PREM_AMT / 12", which
# is precisely the distinction the agent kept getting wrong.
#
# Only the semantic fields are included (type, description, notes). The
# mechanical ones - transformation expressions, value maps, min/max bounds - are
# left out: they cost tokens across every column and say nothing about whether
# two columns mean the same thing.
#
# Caveat worth stating plainly: pasm's written_premium entry names PAS-L's
# ANNU_PREM_AMT directly in its notes. Where this agent succeeds on that pair,
# the catalog is doing much of the work, not the model's own reasoning.

_CATALOG_CACHE: dict[str, dict] = {}


def _load_value_catalog(source_name: str) -> dict:
    """column_name -> catalog entry for a source. Missing or malformed catalogs
    yield {}, so the agent degrades to name-and-samples rather than failing."""
    if source_name in _CATALOG_CACHE:
        return _CATALOG_CACHE[source_name]

    path = os.path.join(_CATALOG_DIR, f"{source_name}_value_catalog.json")
    try:
        with open(path, encoding="utf-8") as f:
            catalog = json.load(f) or {}
        entries = catalog.get("columns", {})
    except Exception:
        entries = {}

    _CATALOG_CACHE[source_name] = entries
    return entries


def _catalog_context(entry: dict | None) -> dict:
    """Semantic fields only - what the column means, not how to transform it."""
    if not entry:
        return {}
    context = {}
    for field_name in ("type", "description", "notes"):
        value = entry.get(field_name)
        if value:
            context[field_name] = value
    return context


def _describe(col: ColumnProfile, catalog: dict | None = None) -> dict:
    """Column description for the prompt: profile stats plus whatever the
    source's value catalog says this column means."""
    described = {
        "name": col.name,
        "type": col.inferred_type,
        "distinct_count": col.distinct_count,
        "null_rate": round(col.null_rate, 3),
        "samples": [str(v) for v in (col.sample_values or [])[:6]],
    }
    context = _catalog_context((catalog or {}).get(col.name))
    if context:
        described["catalog"] = context
    return described


def _extract_json(raw: str) -> dict | None:
    """Pull a JSON object out of a model response, tolerating fences or prose."""
    raw = raw.strip()
    if "```" in raw:
        fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if fenced:
            raw = fenced[-1]
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except Exception:
        return None


def match_unclustered(
    source_a: str,
    columns_a: list[ColumnProfile],
    source_b: str,
    columns_b: list[ColumnProfile],
    min_confidence: float = MIN_AGENT_CONFIDENCE,
    votes: int = 5,
    vote_threshold: float = 0.8,
) -> list[AgentMatch]:
    """Ask the model which leftover columns represent the same concept.

    Returns matches at or above min_confidence. Any failure - no columns, an
    unparseable response, a hallucinated column name - yields fewer matches,
    never wrong ones: the deterministic clusters stand on their own and this
    pass can only add to them.
    """
    if not columns_a or not columns_b:
        return []

    catalog_a = _load_value_catalog(source_a)
    catalog_b = _load_value_catalog(source_b)

    payload = {
        "source_a": {
            "name": source_a,
            "columns": [_describe(c, catalog_a) for c in columns_a],
        },
        "source_b": {
            "name": source_b,
            "columns": [_describe(c, catalog_b) for c in columns_b],
        },
    }

    provider = get_provider("cluster_agent")
    request = dict(
        model=_model(),
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{
            "role": "user",
            "content": (
                "Which of these columns represent the same business concept?\n\n"
                + json.dumps(payload, indent=2)
            ),
        }],
    )

    valid_a = {c.name for c in columns_a}
    valid_b = {c.name for c in columns_b}

    # Vote across repeated runs. The model is not randomly jittering - it is
    # genuinely uncertain, and it shows: written_premium was claimed by
    # MNTHLY_PREM_AMT in 3/5 runs and by ANNU_PREM_AMT in 2/5. A pair the model
    # only proposes some of the time is one it does not actually know, so
    # inconsistency is treated as the abstain signal rather than smoothed over.
    tally: dict[tuple[str, str], list[tuple[float, str]]] = {}
    for _ in range(votes):
        data = _extract_json(call_with_retry(provider, request).text)
        if not data:
            continue
        seen: set[tuple[str, str]] = set()
        for m in (data.get("matches") or []):
            a, b = m.get("a"), m.get("b")
            if a not in valid_a or b not in valid_b:
                continue  # hallucinated column name
            key = (a, b)
            if key in seen:
                continue  # one vote per pair per run
            seen.add(key)
            tally.setdefault(key, []).append((
                float(m.get("confidence") or 0.0),
                str(m.get("reasoning") or "").strip(),
            ))

    required = max(1, int(votes * vote_threshold))
    survivors = [
        (key, entries) for key, entries in tally.items()
        if len(entries) >= required
    ]
    # Strongest first, so a double-assignment resolves in favour of the pair the
    # model was most consistently confident about.
    survivors.sort(
        key=lambda kv: (len(kv[1]), sum(c for c, _ in kv[1]) / len(kv[1])),
        reverse=True,
    )

    claimed_a: set[str] = set()
    claimed_b: set[str] = set()
    matches: list[AgentMatch] = []

    for (a, b), entries in survivors:
        mean_conf = sum(c for c, _ in entries) / len(entries)
        if mean_conf < min_confidence:
            continue
        if a in claimed_a or b in claimed_b:
            continue
        claimed_a.add(a)
        claimed_b.add(b)
        matches.append(AgentMatch(
            source_a=source_a, column_a=a,
            source_b=source_b, column_b=b,
            confidence=round(mean_conf, 3),
            agreement=f"{len(entries)}/{votes}",
            reasoning=entries[0][1],
        ))

    return matches