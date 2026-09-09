"""Cross-source column clustering (MAP-10, step 1).

Given profiles from two or more sources and NO canonical target to anchor
against, group columns that represent the same underlying concept.

This is the inverse of the mapping problem: there is no target field to match
each column to, so equivalence has to be established directly, column-to-column,
across sources.

Scoring is deliberately NOT a weighted sum of every available signal. Shape
agreement - matching types, similar cardinality, similar null rates - is not
semantic evidence. Two string columns of similar cardinality look identical
whether they hold street addresses or tax IDs, so letting shape contribute to
the score lets it manufacture matches on its own. It did, in the first version:
INS_ADDR clustered with insured_ein on shape alone.

So the score is:

    score = semantic_signal * profile_gate

  semantic_signal  what the column actually contains or is called. Value overlap
                   when the columns have a comparable vocabulary (strongest -
                   two coded columns sharing values is near-proof), name
                   similarity otherwise (weakest and actively misleading -
                   WRTG_AGT is the standing reminder that a name can point
                   confidently at the wrong concept).

  profile_gate     a 0-1 multiplier that can only ever REDUCE a score. Wildly
                   incompatible shapes collapse it toward zero; compatible ones
                   leave the semantic signal intact. It vetoes, it never votes.

Clusters hold at most one column per source: a source's own two columns are
different concepts by construction, not candidates for merging.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .models import ColumnProfile, TableProfile

# Minimum score for two columns to share a cluster.
CLUSTER_THRESHOLD = 0.55

# A name match alone, with no value evidence, has to be strong to count -
# this is the WRTG_AGT floor. Below it, a name coincidence contributes nothing.
NAME_FLOOR = 0.62

# Types that should not block a match when the underlying concept is the same.
# Ids and amounts cross these lines constantly between a legacy fixed-width
# extract (POL_NO as integer) and a modern typed platform (policy_id as string).
_TYPE_KIN: dict[str, set[str]] = {
    "integer": {"integer", "decimal", "string"},
    "decimal": {"decimal", "integer", "string"},
    "string": {"string", "integer", "decimal"},
    "date": {"date", "string"},
    "boolean": {"boolean", "string"},
}

# Columns the source system emits about itself, not about the entity. They have
# no counterpart concept in another system's business data.
_METADATA_PREFIXES = ("_cdc", "_meta", "_ingest", "_load")


@dataclass
class ClusterMember:
    source_name: str
    table_name: str
    column: ColumnProfile


@dataclass
class ColumnCluster:
    """A set of columns, at most one per source, judged to mean the same thing."""
    members: list[ClusterMember] = field(default_factory=list)
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    @property
    def sources(self) -> set[str]:
        return {m.source_name for m in self.members}

    def column_names(self) -> list[str]:
        return [f"{m.source_name}.{m.column.name}" for m in self.members]


# ── Semantic signal ──────────────────────────────────────────────────────────

def _value_tokens(col: ColumnProfile) -> set[str]:
    """Comparable value vocabulary, normalized for case and whitespace."""
    tokens: set[str] = set()
    for v in (col.value_distribution or {}):
        tokens.add(str(v).strip().upper())
    for v in (col.sample_values or []):
        tokens.add(str(v).strip().upper())
    return {t for t in tokens if t}


def _value_overlap(a: ColumnProfile, b: ColumnProfile) -> tuple[float, bool]:
    """Jaccard overlap of value vocabularies.

    Returns (score, applicable). Applicable is False where overlap carries no
    information - free text, high-cardinality ids, dates - so a 0.0 is never
    read as evidence of difference. Two policy-id columns from different systems
    legitimately share no values at all.
    """
    a_bounded = a.is_coded_column or len(a.value_distribution or {}) > 0
    b_bounded = b.is_coded_column or len(b.value_distribution or {}) > 0
    if not (a_bounded and b_bounded):
        return 0.0, False

    ta, tb = _value_tokens(a), _value_tokens(b)
    if not ta or not tb:
        return 0.0, False

    union = len(ta | tb)
    return (len(ta & tb) / union if union else 0.0), True


# ── Name normalization ───────────────────────────────────────────────────────
# Legacy extracts abbreviate aggressively; modern platforms spell things out.
# Comparing EFF_DT to effective_date as raw strings measures shared prefix
# characters, not shared meaning - every one of EFF_DT/EXP_DT/POL_NO scored an
# identical 0.667 against its true counterpart, which is a signal that cannot
# tell a real match from a coincidence.
#
# Expanding known abbreviations before comparison makes a true match look like
# a true match (~0.95) while leaving unrelated pairs where they were. This
# raises real matches rather than lowering the bar, so precision is unaffected.
#
# Keep this list conservative: only abbreviations that are unambiguous in a
# data-column context. An expansion that guesses wrong actively creates false
# matches, which is the failure mode this module exists to avoid.
_ABBREVIATIONS: dict[str, str] = {
    # identifiers
    "no": "number", "num": "number", "nbr": "number", "id": "identifier",
    "cd": "code", "seq": "sequence", "ref": "reference", "key": "key",
    # dates
    "dt": "date", "eff": "effective", "exp": "expiration", "term": "termination",
    "ts": "timestamp", "yr": "year", "mo": "month",
    # money
    "amt": "amount", "prem": "premium", "annu": "annual", "mnthly": "monthly",
    "lim": "limit", "ded": "deductible", "bal": "balance",
    # policy / insurance domain
    "pol": "policy", "ins": "insured", "cov": "coverage", "agt": "agent",
    "agcy": "agency", "wrtg": "writing", "uw": "underwriting", "stat": "status",
    "cncl": "cancellation", "rsn": "reason", "carr": "carrier", "prod": "product",
    "chnl": "channel", "regn": "region", "terr": "territory", "rsk": "risk",
    "scr": "score", "addl": "additional", "rel": "relationship",
    "winbk": "win back", "flg": "flag", "lob": "line of business",
    # party / location
    "nm": "name", "addr": "address", "st": "state", "zip": "zip",
    "cust": "customer", "acct": "account", "dist": "distribution",
}


def _expand(token: str) -> str:
    """Expand a single abbreviated token, or return it unchanged."""
    return _ABBREVIATIONS.get(token, token)


def _normalize_name(name: str) -> str:
    """Split a column name into tokens and expand known abbreviations.

    EFF_DT           -> "effective date"
    POL_NO           -> "policy number"
    ANNU_PREM_AMT    -> "annual premium amount"
    effective_date   -> "effective date"
    """
    raw = name.lower().replace("_", " ").replace("-", " ")
    tokens = [t for t in raw.split() if t]
    return " ".join(_expand(t) for t in tokens)


def _name_similarity(a: str, b: str) -> float:
    """Fuzzy name match over abbreviation-expanded tokens."""
    na, nb = _normalize_name(a), _normalize_name(b)
    return max(fuzz.token_set_ratio(na, nb), fuzz.token_sort_ratio(na, nb)) / 100.0


# ── Profile gate ─────────────────────────────────────────────────────────────

def _profile_gate(
    a: ColumnProfile, b: ColumnProfile, rows_a: int, rows_b: int
) -> float:
    """A 0-1 multiplier on the semantic signal. Can only reduce, never add.

    Starts at 1.0 and applies penalties for shape disagreement, so compatible
    columns pass through unchanged and incompatible ones collapse.
    """
    gate = 1.0

    # Unrelated types are close to disqualifying.
    if b.inferred_type not in _TYPE_KIN.get(a.inferred_type, {a.inferred_type}):
        gate *= 0.25
    elif a.inferred_type != b.inferred_type:
        gate *= 0.85  # kin but not identical - mild penalty

    # Distinctness relative to row count. A near-unique id and a 3-value code
    # are not the same concept however similar their names.
    ratio_a = a.distinct_count / rows_a if rows_a else 0.0
    ratio_b = b.distinct_count / rows_b if rows_b else 0.0
    gate *= 1.0 - 0.6 * min(abs(ratio_a - ratio_b), 1.0)

    # Null behaviour. A column that is always populated and one that is mostly
    # empty are unlikely to be the same field.
    gate *= 1.0 - 0.3 * min(abs(a.null_rate - b.null_rate), 1.0)

    # Flag disagreement is a mild penalty; agreement is not a bonus, because
    # agreement on shape is exactly what this function refuses to treat as
    # evidence.
    for flag in ("is_id_column", "is_coded_column", "is_cents_integer"):
        if getattr(a, flag) != getattr(b, flag):
            gate *= 0.90

    # Floor the gate for type-compatible pairs. Compounding three or four mild
    # penalties punishes ordinary cross-system variation (integer vs string ids,
    # different row counts) as harshly as genuine incompatibility.
    type_compatible = b.inferred_type in _TYPE_KIN.get(a.inferred_type, {a.inferred_type})
    floor = 0.72 if type_compatible else 0.0
    return max(gate, floor)


def _is_metadata(col: ColumnProfile) -> bool:
    return col.name.lower().startswith(_METADATA_PREFIXES)


# ── Pair scoring ─────────────────────────────────────────────────────────────

def score_pair(
    a: ColumnProfile, b: ColumnProfile, rows_a: int, rows_b: int
) -> tuple[float, list[str]]:
    """Equivalence score for two columns from different sources.

    semantic * gate. If neither semantic signal fires, the pair scores zero
    regardless of how similar the two columns look structurally.
    """
    # Source metadata has no cross-system counterpart concept.
    if _is_metadata(a) or _is_metadata(b):
        return 0.0, []

    overlap, overlap_applies = _value_overlap(a, b)
    name = _name_similarity(a.name, b.name)

    evidence: list[str] = []

    if overlap_applies and overlap > 0.0:
        # Vocabulary evidence available: lead with it, let a strong name
        # corroborate but not dominate.
        semantic = 0.75 * overlap + 0.25 * name
        evidence.append(f"value overlap {overlap:.2f}")
        if name >= NAME_FLOOR:
            evidence.append(f"name {name:.2f}")
    elif name >= NAME_FLOOR:
        # Name only. Usable, but never at full strength - this is the signal
        # that lies.
        semantic = 0.85 * name
        evidence.append(f"name {name:.2f}")
    else:
        # Nothing semantic to go on. Shape alone is not a match.
        return 0.0, []

    gate = _profile_gate(a, b, rows_a, rows_b)
    if gate < 0.99:
        evidence.append(f"shape gate {gate:.2f}")

    return semantic * gate, evidence


# ── Clustering ───────────────────────────────────────────────────────────────

def cluster_columns(
    tables: list[tuple[str, TableProfile]],
    threshold: float = CLUSTER_THRESHOLD,
) -> list[ColumnCluster]:
    """Group columns across sources into same-concept clusters.

    Greedy agglomeration over cross-source pairs in descending score order. A
    column joins a cluster only if it beats the threshold against every existing
    member and no column from its own source is already there, so a strong pair
    cannot drag a weak third column in behind it.
    """
    members: list[ClusterMember] = []
    row_counts: dict[str, int] = {}
    for source_name, table in tables:
        row_counts[source_name] = table.row_count
        for col in table.columns:
            members.append(ClusterMember(source_name, table.name, col))

    scored: list[tuple[float, int, int, list[str]]] = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            mi, mj = members[i], members[j]
            if mi.source_name == mj.source_name:
                continue  # same source: different concepts by construction
            score, evidence = score_pair(
                mi.column, mj.column,
                row_counts[mi.source_name], row_counts[mj.source_name],
            )
            if score >= threshold:
                scored.append((score, i, j, evidence))

    scored.sort(key=lambda s: s[0], reverse=True)

    # Mutual best match: a pair survives only if each column is the other's
    # best available partner in the other source. Without this, a globally
    # high-scoring pair can claim a column that another column matches better
    # and is now locked out of - TERM_EFF_DT ("termination effective date")
    # outscores EFF_DT against effective_date on token overlap alone, taking
    # the slot EFF_DT genuinely belongs in.
    best_for: dict[int, tuple[float, int]] = {}
    for score, i, j, _ev in scored:
        if score > best_for.get(i, (0.0, -1))[0]:
            best_for[i] = (score, j)
        if score > best_for.get(j, (0.0, -1))[0]:
            best_for[j] = (score, i)

    scored = [
        (s, i, j, ev) for s, i, j, ev in scored
        if best_for.get(i, (0.0, -1))[1] == j and best_for.get(j, (0.0, -1))[1] == i
    ]

    cluster_of: dict[int, ColumnCluster] = {}
    clusters: list[ColumnCluster] = []

    def _fits(cluster: ColumnCluster, idx: int) -> bool:
        cand = members[idx]
        if cand.source_name in cluster.sources:
            return False
        for m in cluster.members:
            s, _ = score_pair(
                m.column, cand.column,
                row_counts[m.source_name], row_counts[cand.source_name],
            )
            if s < threshold:
                return False
        return True

    for score, i, j, evidence in scored:
        ci, cj = cluster_of.get(i), cluster_of.get(j)

        if ci is None and cj is None:
            cluster = ColumnCluster(
                members=[members[i], members[j]],
                confidence=round(score, 3),
                evidence=evidence,
            )
            clusters.append(cluster)
            cluster_of[i] = cluster_of[j] = cluster
        elif ci is not None and cj is None:
            if _fits(ci, j):
                ci.members.append(members[j])
                cluster_of[j] = ci
                ci.confidence = round(min(ci.confidence, score), 3)
        elif ci is None and cj is not None:
            if _fits(cj, i):
                cj.members.append(members[i])
                cluster_of[i] = cj
                cj.confidence = round(min(cj.confidence, score), 3)
        # Both already clustered: leave them. Merging two established clusters
        # on one cross pair is how unrelated concepts get chained together.

    # Columns with no cross-source match are concepts unique to one source. They
    # still belong in a synthesized target, so they survive as clusters of one.
    for idx, m in enumerate(members):
        if idx not in cluster_of:
            clusters.append(ColumnCluster(
                members=[m], confidence=0.0, evidence=["no cross-source match"],
            ))

    clusters.sort(key=lambda c: (-len(c.members), -c.confidence))
    return clusters