"""Generate a schema-drift PAS-L variant fixture — for testing MAP-4 Layer 1's
retrieval generalization, NOT a full WP-10 implementation.

docs/self-tuning-mapper-agent-plan.md's testing-adequacy discussion flagged a
real gap: PAS-L has exactly one column set, so accumulating mapping_history
across runs just re-cites the SAME 46 column names every time. That tests
curation (does the bank fill up correctly?) but not retrieval generalization
(does similarity scoring correctly match a NEW, differently-named column to a
past example of the same underlying semantics — the actual point of a
few-shot bank, as opposed to a lookup table keyed by exact name?).

This script renames a deliberately-chosen subset of PAS-L's columns (the
is_hard ones, where generalization matters most) to plausible alternate
names a sibling extract or a later schema revision might use, and writes a
second .dat file with the same rows under those new headers. It does NOT
build a parallel ground-truth catalog or wire into generate_baseline.py /
generate_simulation.py — full schema-drift support (WP-10, two snapshots,
breaking-change detection via tracker.py) is a separate, larger undertaking;
see docs/mapper-agent-roadmap.md (MAP-6 build order discussion) before
attempting that.

Usage:
    python tools/generate_schema_drift_variant.py
    python tools/generate_schema_drift_variant.py --input path/to/file.dat --output path/to/variant.dat
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = _REPO_ROOT / "schema_inference" / "test_data" / "pasl_policy.dat"
DEFAULT_OUTPUT = _REPO_ROOT / "schema_inference" / "test_data" / "pasl_policy_schema_drift.dat"

# Renames target PAS-L's is_hard columns specifically (ground_truth/pasl_schema_catalog.yml)
# — these are exactly the columns where Layer 1's few-shot bank is supposed to help, so
# they're the most useful ones to test generalization against. Plausible alternate names
# a sibling extract or later schema revision might use for the same underlying column.
RENAME_MAP = {
    "WRTG_AGT":      "WRTNG_AGT_CD",
    "INS_ST":        "INSD_ST_CD",
    "PROD_CD":       "PRODUCT_CD",
    "ANNU_PREM_AMT": "ANN_PREM_AMT",
    "MNTHLY_PREM_AMT": "MO_PREM_AMT",
    "PRIOR_CARR_CD": "PRIOR_CARRIER_CD",
    "WINBK_FLG":     "WIN_BACK_FLG",
}


def generate(input_path: Path, output_path: Path, rename_map: dict[str, str]) -> tuple[list[str], list[str]]:
    """Returns (renamed, not_found) column name lists for reporting."""
    lines = input_path.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError(f"{input_path} is empty")

    headers = lines[0].split("|")
    renamed: list[str] = []
    not_found = list(rename_map.keys())
    new_headers = []
    for h in headers:
        if h in rename_map:
            new_headers.append(rename_map[h])
            renamed.append(h)
            not_found.remove(h)
        else:
            new_headers.append(h)

    out_lines = ["|".join(new_headers)] + lines[1:]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return renamed, not_found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        sys.exit(f"Error: input file not found: {input_path}")

    renamed, not_found = generate(input_path, Path(args.output), RENAME_MAP)

    print(f"Wrote {args.output}")
    print(f"Renamed {len(renamed)} column(s):")
    for old in renamed:
        print(f"  {old} -> {RENAME_MAP[old]}")
    if not_found:
        print(f"Not found in source (skipped): {not_found}")


if __name__ == "__main__":
    main()
