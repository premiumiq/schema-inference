"""Schema Profiler — streams flat files and produces a SchemaProfile.

Handles pipe- and comma-delimited .dat/.csv files. Never loads the full file
into memory: one row at a time, per-column counters only.

Type-inference order (first match wins per cell):
  YYYYMMDD date → ISO8601 date → US date → boolean → integer → decimal → string

Empty strings count as null (PAS-L style). CRLF-safe via newline=''.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .models import ColumnProfile, SchemaProfile, TableProfile

# Max distinct values tracked per column (beyond this, stop inserting into the set)
DISTINCT_CAP = 1_000
# Max rows profiled for type inference (row_count reflects true file size)
PROFILE_ROW_LIMIT = 50_000
# Sample reservoir size (non-null values kept)
SAMPLE_SIZE = 10
# Top-N for value_distribution
TOP_N = 5

_YYYYMMDD_RE = re.compile(r"^\d{8}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_US_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_BOOL_VALUES = frozenset({"y", "n", "true", "false", "yes", "no", "t", "f"})
_ID_SUFFIX_RE = re.compile(r"(_ID|_NO|_NBR|_SEQ|_NUM|_KEY|_REF)$", re.IGNORECASE)
_CENTS_SUFFIX_RE = re.compile(
    r"(AMT|PREM|LIM|DED|VAL|REV|EXPO|COST|FEE|CHG|BAL)", re.IGNORECASE
)
_CDC_PREFIX_RE = re.compile(r"^_CDC_", re.IGNORECASE)


def _probe_cell(value: str) -> tuple[str, str | None]:
    """Return (inferred_type, date_format|None) for a single non-empty cell value."""
    v = value.strip()

    # YYYYMMDD — must come before integer (20240115 looks like an integer)
    if _YYYYMMDD_RE.match(v):
        yr, mo, dy = int(v[:4]), int(v[4:6]), int(v[6:])
        if 1900 <= yr <= 2099 and 1 <= mo <= 12 and 1 <= dy <= 31:
            return "date", "YYYYMMDD"

    # ISO 8601
    if _ISO_DATE_RE.match(v):
        parts = v.split("-")
        if 1 <= int(parts[1]) <= 12 and 1 <= int(parts[2]) <= 31:
            return "date", "ISO8601"

    # US date
    if _US_DATE_RE.match(v):
        return "date", "US"

    # Boolean Y/N / true/false strings (not 1/0 — ambiguous with integer)
    if v.lower() in _BOOL_VALUES:
        return "boolean", None

    # Integer (includes negative)
    stripped = v.lstrip("-")
    if stripped and stripped.isdigit():
        return "integer", None

    # Decimal
    if "." in v:
        try:
            float(v)
            return "decimal", None
        except ValueError:
            pass

    return "string", None


def _detect_delimiter(first_line: str, filename: str) -> str:
    pipe_count = first_line.count("|")
    comma_count = first_line.count(",")
    if pipe_count >= 2:
        return "|"
    if comma_count >= 2:
        return ","
    return "|" if filename.lower().endswith(".dat") else ","


class _ColStats:
    """Per-column accumulator."""

    __slots__ = (
        "null_count", "total_count",
        "type_votes", "date_format_votes",
        "distinct_set", "distinct_count",
        "counter", "samples",
    )

    def __init__(self) -> None:
        self.null_count = 0
        self.total_count = 0
        self.type_votes: Counter[str] = Counter()
        self.date_format_votes: Counter[str] = Counter()
        self.distinct_set: set[str] = set()
        self.distinct_count = 0
        self.counter: Counter[str] = Counter()
        self.samples: list[str] = []

    def feed(self, value: str, within_limit: bool) -> None:
        self.total_count += 1
        v = value.strip()

        if not v:
            self.null_count += 1
            return

        # Distinct tracking (capped)
        if len(self.distinct_set) < DISTINCT_CAP:
            self.distinct_set.add(v)
        self.distinct_count += 1  # raw count of non-null values (may have dupes)

        # Counter (for top-N distribution)
        self.counter[v] += 1

        # Sample reservoir (first SAMPLE_SIZE non-null)
        if len(self.samples) < SAMPLE_SIZE:
            self.samples.append(v)

        if not within_limit:
            return

        t, dfmt = _probe_cell(v)
        self.type_votes[t] += 1
        if dfmt:
            self.date_format_votes[dfmt] += 1

    def finalize(self, col_name: str) -> ColumnProfile:
        total = self.total_count or 1
        null_rate = self.null_count / total

        # Winning type
        inferred_type = self.type_votes.most_common(1)[0][0] if self.type_votes else "string"

        # Date format (majority of date votes)
        date_format: str | None = None
        if inferred_type == "date" and self.date_format_votes:
            date_format = self.date_format_votes.most_common(1)[0][0]

        # Distinct count: size of set if under cap, else DISTINCT_CAP (conservative)
        true_distinct = len(self.distinct_set)

        # Value distribution: only when few distinct values
        value_dist: dict[str, int] = {}
        if true_distinct <= 20:
            value_dist = dict(self.counter.most_common(TOP_N))

        # ID column detection
        is_id = bool(_ID_SUFFIX_RE.search(col_name))

        # Coded column: few distinct values, short strings
        max_val_len = max((len(v) for v in self.distinct_set), default=0)
        is_coded = (
            true_distinct < 20
            and inferred_type in ("string", "boolean")
            and max_val_len <= 10
        )

        # Cents integer: integer column whose name contains financial keywords
        is_cents = inferred_type == "integer" and bool(_CENTS_SUFFIX_RE.search(col_name))

        return ColumnProfile(
            name=col_name,
            inferred_type=inferred_type,
            null_rate=round(null_rate, 4),
            distinct_count=true_distinct,
            sample_values=self.samples[:SAMPLE_SIZE],
            value_distribution=value_dist,
            date_format=date_format,
            is_id_column=is_id,
            is_coded_column=is_coded,
            is_cents_integer=is_cents,
        )


def profile_file(
    file_path: str | Path,
    source_name: str,
    table_name: str | None = None,
    delimiter: str | None = None,
) -> SchemaProfile:
    """Stream a flat file and return a SchemaProfile.

    Args:
        file_path:   Path to .dat or .csv file.
        source_name: Logical source system name (e.g. "pasl", "broker_abc").
        table_name:  Override table name; defaults to filename stem.
        delimiter:   Column delimiter; auto-detected if None.
    """
    path = Path(file_path)
    tname = table_name or path.stem

    with open(path, "r", newline="", encoding="utf-8") as fh:
        first_line = fh.readline()

    detected_delim = delimiter or _detect_delimiter(first_line, path.name)

    stats: dict[str, _ColStats] = {}
    headers: list[str] = []
    row_count = 0

    with open(path, "r", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh, delimiter=detected_delim)
        for i, row in enumerate(reader):
            if i == 0:
                headers = [h.strip() for h in row]
                stats = {h: _ColStats() for h in headers}
                continue

            row_count += 1
            within_limit = row_count <= PROFILE_ROW_LIMIT

            for j, col in enumerate(headers):
                value = row[j] if j < len(row) else ""
                stats[col].feed(value, within_limit)

    columns = [stats[h].finalize(h) for h in headers]

    # Detect empty-string-null convention: if >5% of non-zero-null-rate columns
    # have null_rate driven by empty strings rather than absent cells.
    # For flat files all missing values are empty strings, so always True.
    is_empty_string_null = True

    table = TableProfile(
        name=tname,
        row_count=row_count,
        columns=columns,
        delimiter=detected_delim,
        source_file=path.name,
        is_empty_string_null=is_empty_string_null,
    )

    # Profile hash: SHA256 of sorted "col_name:type" fingerprints
    fingerprint = sorted(f"{c.name}:{c.inferred_type}" for c in columns)
    profile_hash = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()[:16]

    return SchemaProfile(
        source_name=source_name,
        tables=[table],
        profiled_at=datetime.now(),
        profile_hash=profile_hash,
    )
