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
import re
from dataclasses import dataclass

from ..llm.registry import get_provider, model_for
from ..models import ColumnProfile
from .throttle import call_with_retry

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
    reasoning: str

    def as_pair(self) -> tuple[tuple[str, str], tuple[str, str]]:
        return ((self.source_a, self.column_a), (self.source_b, self.column_b))


def _model() -> str:
    return model_for("cluster_agent") or MODEL


def _describe(col: ColumnProfile) -> dict:
    """Compact column description for the prompt. Sample values are the point -
    these columns are here because their names were not enough."""
    return {
        "name": col.name,
        "type": col.inferred_type,
        "distinct_count": col.distinct_count,
        "null_rate": round(col.null_rate, 3),
        "samples": [str(v) for v in (col.sample_values or [])[:6]],
    }


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
) -> list[AgentMatch]:
    """Ask the model which leftover columns represent the same concept.

    Returns matches at or above min_confidence. Any failure - no columns, an
    unparseable response, a hallucinated column name - yields fewer matches,
    never wrong ones: the deterministic clusters stand on their own and this
    pass can only add to them.
    """
    if not columns_a or not columns_b:
        return []

    payload = {
        "source_a": {"name": source_a, "columns": [_describe(c) for c in columns_a]},
        "source_b": {"name": source_b, "columns": [_describe(c) for c in columns_b]},
    }

    provider = get_provider("cluster_agent")
    response = call_with_retry(provider, dict(
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
    ))

    data = _extract_json(response.text)
    if not data:
        return []

    valid_a = {c.name for c in columns_a}
    valid_b = {c.name for c in columns_b}
    claimed_a: set[str] = set()
    claimed_b: set[str] = set()
    matches: list[AgentMatch] = []

    # Highest confidence first, so if the model double-assigns despite the
    # instruction, the stronger claim wins rather than whichever came first.
    raw_matches = data.get("matches") or []
    raw_matches.sort(key=lambda m: float(m.get("confidence") or 0.0), reverse=True)

    for m in raw_matches:
        a, b = m.get("a"), m.get("b")
        conf = float(m.get("confidence") or 0.0)

        # Reject hallucinated names outright rather than trying to fuzzy-match
        # them back to a real column.
        if a not in valid_a or b not in valid_b:
            continue
        if conf < min_confidence:
            continue
        if a in claimed_a or b in claimed_b:
            continue

        claimed_a.add(a)
        claimed_b.add(b)
        matches.append(AgentMatch(
            source_a=source_a, column_a=a,
            source_b=source_b, column_b=b,
            confidence=round(conf, 3),
            reasoning=str(m.get("reasoning") or "").strip(),
        ))

    return matches