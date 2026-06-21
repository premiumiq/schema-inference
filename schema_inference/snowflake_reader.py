"""Snowflake reader — profile a table directly from Snowflake.

Mirrors profiler.profile_file() but sources rows from a Snowflake query instead
of a flat file. Reuses the same _ColStats / _probe_cell profiling logic, so the
resulting SchemaProfile is identical in shape to a file-based profile.

Connection uses RSA key-pair auth, matching the project's .env convention:
  SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY_PATH,
  SNOWFLAKE_WAREHOUSE, SNOWFLAKE_ROLE
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime

from .models import SchemaProfile, TableProfile
from .profiler import PROFILE_ROW_LIMIT, _ColStats


def _load_private_key(key_path: str):
    """Load an RSA private key (.p8) for Snowflake key-pair auth."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    with open(key_path, "rb") as f:
        p_key = serialization.load_pem_private_key(
            f.read(),
            password=passphrase.encode() if passphrase else None,
            backend=default_backend(),
        )
    return p_key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _connect():
    """Open a Snowflake connection.

    Auth modes, in priority order:
    - SNOWFLAKE_PRIVATE_KEY_PATH set -> key-pair auth (service accounts)
    - SNOWFLAKE_PASSWORD set         -> username/password auth
    - otherwise                      -> external browser SSO
    """
    import snowflake.connector

    account = os.environ["SNOWFLAKE_ACCOUNT"]
    user = os.environ["SNOWFLAKE_USER"]
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE")
    role = os.environ.get("SNOWFLAKE_ROLE")
    database = os.environ.get("SNOWFLAKE_DATABASE")

    key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH")

    if key_path:
        return snowflake.connector.connect(
            account=account, user=user,
            private_key=_load_private_key(key_path),
            warehouse=warehouse, role=role, database=database,
        )

    return snowflake.connector.connect(
        account=account, user=user, authenticator="externalbrowser",
        warehouse=warehouse, role=role, database=database,
    )


def profile_snowflake_table(
    database: str,
    schema: str,
    table: str,
    source_name: str,
    table_name: str | None = None,
    row_limit: int = PROFILE_ROW_LIMIT,
) -> SchemaProfile:
    """Profile a Snowflake table directly. Returns a SchemaProfile identical in
    shape to a file-based profile.

    Args:
        database:    Snowflake database (e.g. "DEV_SANDBOX_DB").
        schema:      Snowflake schema (e.g. "EXP_SCHEMAINFERENCE").
        table:       Table name (e.g. "pasl_policy").
        source_name: Logical source name for the profile header.
        table_name:  Override table name; defaults to the Snowflake table name.
        row_limit:   Max rows to fetch for profiling.
    """
    tname = table_name or table.lower()

    conn = _connect()
    try:
        cur = conn.cursor()
        fqtn = f'"{database}"."{schema}"."{table}"'

        # True row count (independent of the profiling fetch limit)
        cur.execute(f"SELECT COUNT(*) FROM {fqtn}")
        true_row_count = cur.fetchone()[0]

        cur.execute(f"SELECT * FROM {fqtn} LIMIT {row_limit}")

        # Column names from the cursor description
        headers = [desc[0] for desc in cur.description]
        stats: dict[str, _ColStats] = {h: _ColStats() for h in headers}

        row_count = 0
        for row in cur:
            row_count += 1
            within_limit = row_count <= row_limit
            for j, col in enumerate(headers):
                value = row[j]
                # Normalize everything to string, like the file reader sees.
                # None (true SQL NULL) and empty string both count as null.
                if value is None:
                    sval = ""
                else:
                    sval = str(value)
                stats[col].feed(sval, within_limit)
    finally:
        conn.close()

    columns = [stats[h].finalize(h) for h in headers]

    table_profile = TableProfile(
        name=tname,
        row_count=true_row_count,
        columns=columns,
        delimiter="",                 # not applicable for DB source
        source_file=f"{database}.{schema}.{table}",
        is_empty_string_null=True,    # we mapped SQL NULL -> "" above
    )

    fingerprint = sorted(f"{c.name}:{c.inferred_type}" for c in columns)
    profile_hash = hashlib.sha256(json.dumps(fingerprint).encode()).hexdigest()[:16]

    return SchemaProfile(
        source_name=source_name,
        tables=[table_profile],
        profiled_at=datetime.now(),
        profile_hash=profile_hash,
    )