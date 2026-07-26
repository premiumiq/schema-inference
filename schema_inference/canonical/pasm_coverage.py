"""Canonical PAS-M coverage field definitions — mirrors insurance_data_ecosystem's
dbt_project/insurance_multi_pas/models/staging/pas_m/stg_pasm_coverage.sql column
output. No separate silver-layer model exists yet for this table, so the staging
model's CAST'd output is the authoritative target shape.

required = the three columns with not_null dbt tests in stg_pasm_coverage's
schema.yml (policy_id, coverage_seq, coverage_code).
"""

from __future__ import annotations

from .policy import CanonicalField

CANONICAL_FIELDS: list[CanonicalField] = [
    CanonicalField(
        name="policy_id",
        target_type="integer",
        required=True,
        description="Integer policy identifier for the covered policy (FK to policy.policy_id). Source prefix PCM- stripped.",
        aliases=["pol_id", "pol_no", "policy_no", "policy_number"],
    ),
    CanonicalField(
        name="coverage_seq",
        target_type="integer",
        required=True,
        description="Sequence number of this coverage line within the policy.",
        aliases=["coverage_sequence", "cov_seq", "coverage_line"],
    ),
    CanonicalField(
        name="coverage_code",
        target_type="string",
        required=True,
        description="Coded coverage type (e.g. XS_AUTO, XS_HOME).",
        aliases=["cov_code", "coverage_type", "cov_cd"],
    ),
    CanonicalField(
        name="coverage_name",
        target_type="string",
        required=False,
        description="Human-readable coverage name.",
        aliases=["cov_name", "coverage_desc", "coverage_description"],
    ),
    CanonicalField(
        name="limit_per_occurrence",
        target_type="decimal",
        required=False,
        description="Coverage limit per occurrence.",
        aliases=["occ_limit", "per_occ_limit", "limit_occ"],
    ),
    CanonicalField(
        name="limit_aggregate",
        target_type="decimal",
        required=False,
        description="Coverage aggregate limit.",
        aliases=["agg_limit", "aggregate_limit"],
    ),
    CanonicalField(
        name="deductible",
        target_type="decimal",
        required=False,
        description="Coverage deductible amount.",
        aliases=["ded", "ded_amt", "deductible_amount"],
    ),
    CanonicalField(
        name="coinsurance_pct",
        target_type="decimal",
        required=False,
        description="Coinsurance percentage.",
        aliases=["coins_pct", "coinsurance", "coins"],
    ),
    CanonicalField(
        name="premium_allocation",
        target_type="decimal",
        required=False,
        description="Portion of policy premium allocated to this coverage line.",
        aliases=["prem_alloc", "allocated_premium"],
    ),
    CanonicalField(
        name="effective_date",
        target_type="date",
        required=False,
        description="Coverage effective date.",
        aliases=["eff_dt", "eff_date", "start_date"],
    ),
    CanonicalField(
        name="expiration_date",
        target_type="date",
        required=False,
        description="Coverage expiration date.",
        aliases=["exp_dt", "exp_date", "end_date"],
    ),
]

CANONICAL_BY_NAME: dict[str, CanonicalField] = {f.name: f for f in CANONICAL_FIELDS}
CANONICAL_NAMES: frozenset[str] = frozenset(CANONICAL_BY_NAME)
