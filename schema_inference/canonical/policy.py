"""Canonical policy field definitions — mirrors slv_policy silver table columns.

These are the target fields the mapper proposes mappings to. The list is derived
from slv_policy.sql + stg_pas_policy.sql column output. FK surrogate IDs
(channel_id, product_id, region_id) are replaced here with their source code
equivalents (channel_code, product_code, region_code) because staging models
receive raw codes, not resolved integers.

Adding new aliases: aliases should be lowercase and cover known PAS abbreviations,
vendor naming conventions, and common industry shorthands. More aliases = better
recall in the rule-based pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CanonicalField:
    name: str
    target_type: str        # integer | bigint | string | decimal | date | boolean
    required: bool
    description: str
    aliases: list[str] = field(default_factory=list)

    def all_names(self) -> list[str]:
        return [self.name] + self.aliases


CANONICAL_FIELDS: list[CanonicalField] = [
    CanonicalField(
        name="policy_id",
        target_type="integer",
        required=True,
        description="Integer policy identifier (natural key). Source prefix like PCL-/PCM- stripped.",
        aliases=["pol_no", "pol_num", "pol_id", "policy_no", "policy_number", "pol_ref",
                 "sys_pol_id", "pol_nbr", "policy_nbr", "policyid", "policy_key"],
    ),
    CanonicalField(
        name="policy_number",
        target_type="string",
        required=True,
        description="Human-readable policy number (may include system prefix: PCL-, PCM-).",
        aliases=["pol_num", "pol_no", "policy_no", "pol_ref", "policy_ref",
                 "policy_num", "pol_number"],
    ),
    CanonicalField(
        name="customer_id",
        target_type="bigint",
        required=True,
        description="Insured customer identifier.",
        aliases=["insrd_id", "insured_id", "cust_id", "customer_no", "insrd_no",
                 "customer_number", "client_id", "insured_no", "acct_id", "sf_acct_id"],
    ),
    CanonicalField(
        name="agent_id",
        target_type="bigint",
        required=True,
        description="Producing agent identifier.",
        aliases=["prod_agt_id", "agt_id", "agent_no", "agent_number", "prod_agt_no",
                 "agt_no", "producing_agent_id", "agent_num", "agt_num"],
    ),
    CanonicalField(
        name="channel_code",
        target_type="string",
        required=False,
        description="Distribution channel code.",
        aliases=["dist_chnl_cd", "distribution_channel", "dist_cd", "chnl_cd",
                 "channel_id", "dist_recd", "dist_channel", "channel", "chnl"],
    ),
    CanonicalField(
        name="product_code",
        target_type="string",
        required=True,
        description="Product code (PA-STD, HO-STD, CGL-STD, WC-STD, etc.).",
        aliases=["prod_cd", "product_id", "prod_id", "product_num", "product",
                 "lob_cd", "line_of_business", "product_type"],
    ),
    CanonicalField(
        name="region_code",
        target_type="string",
        required=False,
        description="Geographic region/territory code.",
        aliases=["regn_cd", "region_id", "regn_id", "terr_cd", "territory_code",
                 "region", "territory", "geo_cd", "territory_id"],
    ),
    CanonicalField(
        name="policy_type",
        target_type="string",
        required=True,
        description="Policy type classification (Personal/Commercial).",
        aliases=["pol_typ_cd", "policy_typ", "pol_type_cd", "insured_type",
                 "pol_type", "policy_category", "type_cd"],
    ),
    CanonicalField(
        name="start_date",
        target_type="date",
        required=True,
        description="Policy effective / inception date.",
        aliases=["eff_dt", "effective_date", "pol_eff_dt", "eff_date", "policy_start",
                 "inception_date", "incp_dt", "start_dt", "policy_effective_date",
                 "pol_start_dt", "effective_dt"],
    ),
    CanonicalField(
        name="end_date",
        target_type="date",
        required=True,
        description="Policy expiration / termination date.",
        aliases=["exp_dt", "expiration_date", "pol_exp_dt", "exp_date", "policy_end",
                 "expiry_date", "term_dt", "end_dt", "policy_expiration_date",
                 "pol_end_dt", "expiry_dt"],
    ),
    CanonicalField(
        name="premium_amount",
        target_type="decimal",
        required=True,
        description="Written premium in dollars. PAS-L stores as integer cents; divide by 100.",
        aliases=["tot_prem_amt", "wrt_prem_amt", "written_premium", "premium",
                 "prem_amt", "total_premium", "annual_premium", "gross_prem_amt",
                 "tot_written_prem", "premium_amount", "prem"],
    ),
    CanonicalField(
        name="coverage_limit",
        target_type="decimal",
        required=False,
        description="Total coverage limit.",
        aliases=["tot_cov_lim", "total_coverage_limit", "cov_lim", "limit",
                 "coverage_amount", "limit_aggregate", "cov_lim_agg", "max_cov_lim",
                 "total_limit", "pol_limit"],
    ),
    CanonicalField(
        name="policy_deductible",
        target_type="decimal",
        required=False,
        description="Policy deductible amount.",
        aliases=["pol_ded_amt", "deductible", "ded_amt", "policy_ded",
                 "pol_deductible", "deductible_amount", "ded"],
    ),
    CanonicalField(
        name="policy_aggregate_deductible",
        target_type="decimal",
        required=False,
        description="Annual aggregate deductible cap. NULL = no aggregate cap.",
        aliases=["pol_agg_ded_amt", "aggregate_deductible", "agg_ded_amt",
                 "annual_agg_ded", "agg_deductible"],
    ),
    CanonicalField(
        name="policy_status",
        target_type="string",
        required=True,
        description="Policy status (Active / Cancelled / Expired / Pending).",
        aliases=["pol_stat_cd", "status", "stat_cd", "policy_stat",
                 "pol_status", "status_code", "pol_stat"],
    ),
    CanonicalField(
        name="annual_revenue_impact",
        target_type="decimal",
        required=False,
        description="Estimated annual revenue impact of this policy.",
        aliases=["annl_rev_impact", "revenue_impact", "rev_impact", "annual_revenue",
                 "rev_amt", "annual_rev_impact"],
    ),
    CanonicalField(
        name="risk_score",
        target_type="integer",
        required=False,
        description="Underwriting risk score (0–100).",
        aliases=["rsk_scr", "risk_scr", "uw_risk_score", "risk_scr",
                 "rsk_score", "uw_score", "risk", "underwriting_score"],
    ),
    CanonicalField(
        name="policy_indicator",
        target_type="string",
        required=False,
        description="Policy indicator / classification flag.",
        aliases=["pol_ind_cd", "indicator", "pol_indicator", "indicator_code",
                 "pol_ind", "policy_ind"],
    ),
    CanonicalField(
        name="original_policy_id",
        target_type="integer",
        required=False,
        description="Original policy ID for rewritten or replaced policies.",
        aliases=["orig_pol_id", "original_policy", "prior_pol_id", "orig_pol_no",
                 "orig_policy_id", "rewrite_from_id"],
    ),
    CanonicalField(
        name="renewability_status",
        target_type="string",
        required=False,
        description="Renewal eligibility status.",
        aliases=["rnwl_stat_cd", "renewability_status", "renewal_status",
                 "rnwl_stat", "renew_stat_cd", "renewal_eligibility"],
    ),
    CanonicalField(
        name="lapse_reason",
        target_type="string",
        required=False,
        description="Reason code for policy lapse.",
        aliases=["lapse_rsn_cd", "lapse_reason", "lapse_rsn", "nonrenewal_reason",
                 "lapse_reason_code", "lapse_cd"],
    ),
    CanonicalField(
        name="prior_carrier",
        target_type="string",
        required=False,
        description="Prior insurance carrier name.",
        aliases=["prior_carr_nm", "prior_carrier_name", "prior_carr", "prev_carrier",
                 "prior_carrier_code", "prior_carr_cd", "previous_carrier"],
    ),
    CanonicalField(
        name="cancel_reason",
        target_type="string",
        required=False,
        description="Reason code for policy cancellation.",
        aliases=["cncl_rsn_cd", "cancellation_reason", "cancel_rsn_cd", "cnl_reason",
                 "cancel_reason_code", "cncl_reason", "cancel_cd"],
    ),
    CanonicalField(
        name="win_back_eligible",
        target_type="boolean",
        required=False,
        description="Whether the insured is eligible for win-back outreach.",
        aliases=["winback_flg", "winbk_flg", "win_back_flag", "winback_eligible",
                 "win_back", "wb_eligible", "wb_flg"],
    ),
]

# Quick lookup by name
CANONICAL_BY_NAME: dict[str, CanonicalField] = {f.name: f for f in CANONICAL_FIELDS}
CANONICAL_NAMES: frozenset[str] = frozenset(CANONICAL_BY_NAME)
