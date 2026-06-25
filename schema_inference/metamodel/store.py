"""
store.py — SQLite-backed history store for the mapper agent pipeline (MAP-1).

Tracks every mapping decision, every scoring run's loss, and (for MAP-4) the
prompt-version history of the agents. Mirrors .claude/runtime/store.py's
pattern: SQLite WAL mode, thread lock, UUID keys, ISO timestamps, and
open_store() returns None instead of raising — state is optional and must
never block the mapping/review/scoring pipeline.

Database location: schema_inference/metamodel/metamodel.db (gitignored —
generated state, like .agent/state.db).

TODO (future): migrate mapping_history / loss_runs / prompt_versions from
SQLite to dbt-managed tables (Snowflake or this Postgres warehouse) once
tuning runs accumulate real cross-client volume. Keep this module's method
signatures stable across that migration — swap the backing implementation,
not the call sites in orchestrator.py / evaluator_agent.py / reviewer.py.

Public API:
    MetamodelStore(db_path)
        .record_mapping(run_id, source_name, table_name, source_column,
                         target_field, confidence, method, sql_expression,
                         verdict=None, reviewer_action=None, notes="",
                         profile_signature=None) -> str
        .update_mapping_verdict(run_id, source_column, verdict) -> int
        .update_mapping_review(run_id, source_column, reviewer_action) -> int
        .has_run(run_id) -> bool
        .get_mapping_history(source_name, table_name=None, source_column=None, limit=50) -> list[dict]
        .record_loss_run(run_id, source_name, table_name, metrics, config_snapshot) -> str
        .get_loss_runs(source_name, limit=50) -> list[dict]
        .record_prompt_version(agent_name, prompt_text, parent_version_id=None,
                                loss_before=None, loss_after=None, accepted=False) -> str
        .get_prompt_versions(agent_name, accepted_only=False) -> list[dict]
        .add_few_shot_example(source_name, source_column, target_field, sql_expression,
                              reasoning, profile_signature, origin) -> str          (MAP-4 Layer 1)
        .get_few_shot_examples(source_name, status="active") -> list[dict]         (MAP-4 Layer 1)
        .has_few_shot_example(source_name, source_column, origin) -> bool          (MAP-4 Layer 1)
        .retire_few_shot_example(example_id, reason) -> int                        (MAP-4 Layer 1)
        .close()

    open_store(db_path) -> MetamodelStore | None
        Returns None (not raises) if DB cannot be opened — callers treat
        history as optional.

    default_path() -> Path
        schema_inference/metamodel/metamodel.db
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uid() -> str:
    return str(uuid.uuid4())


def _row(cursor: sqlite3.Cursor) -> dict | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in cursor.description], row))


def _rows(cursor: sqlite3.Cursor) -> list[dict]:
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in cursor.fetchall()]


def default_path() -> Path:
    return Path(__file__).parent / "metamodel.db"


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS mapping_history (
    id              TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    source_name     TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    source_column   TEXT NOT NULL,
    target_field    TEXT,
    confidence      REAL NOT NULL,
    method          TEXT NOT NULL,
    sql_expression  TEXT NOT NULL,
    verdict         TEXT,
    reviewer_action TEXT,
    notes                   TEXT,
    profile_signature_json  TEXT,
    recorded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mh_source_col ON mapping_history(source_name, source_column);
CREATE INDEX IF NOT EXISTS idx_mh_run        ON mapping_history(run_id);

CREATE TABLE IF NOT EXISTS few_shot_examples (
    example_id              TEXT PRIMARY KEY,
    source_name             TEXT NOT NULL,
    source_column           TEXT NOT NULL,
    target_field            TEXT,
    sql_expression          TEXT NOT NULL,
    reasoning               TEXT NOT NULL,
    profile_signature_json  TEXT NOT NULL,
    origin                  TEXT NOT NULL,   -- 'hard_tp' | 'critic_override_accepted'
    status                  TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'retired'
    added_at                TEXT NOT NULL,
    retired_at              TEXT,
    retire_reason           TEXT
);
CREATE INDEX IF NOT EXISTS idx_fse_source ON few_shot_examples(source_name, status);

CREATE TABLE IF NOT EXISTS loss_runs (
    run_id               TEXT PRIMARY KEY,
    source_name          TEXT NOT NULL,
    table_name           TEXT NOT NULL,
    metrics_json         TEXT NOT NULL,
    config_snapshot_json TEXT NOT NULL,
    recorded_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lr_source ON loss_runs(source_name, recorded_at);

CREATE TABLE IF NOT EXISTS prompt_versions (
    version_id        TEXT PRIMARY KEY,
    agent_name         TEXT NOT NULL,
    prompt_text        TEXT NOT NULL,
    parent_version_id  TEXT,
    loss_before        REAL,
    loss_after         REAL,
    accepted           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pv_agent ON prompt_versions(agent_name, accepted);
"""


# ── MetamodelStore ────────────────────────────────────────────────────────────

class MetamodelStore:
    """Thin wrapper around a SQLite connection. All methods are synchronous."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(_DDL)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent ALTER TABLE for columns added after a DB already existed
        on disk. CREATE TABLE IF NOT EXISTS doesn't add columns to a table
        that already exists with an older shape — this does. Safe to call on
        every open(); ignores 'duplicate column' errors."""
        for ddl in (
            "ALTER TABLE mapping_history ADD COLUMN notes TEXT",
            "ALTER TABLE mapping_history ADD COLUMN profile_signature_json TEXT",
        ):
            try:
                self._conn.execute(ddl)
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise

    def close(self) -> None:
        self._conn.close()

    # ── mapping_history ───────────────────────────────────────────────────────

    def record_mapping(
        self,
        run_id:            str,
        source_name:       str,
        table_name:        str,
        source_column:     str,
        target_field:      str | None,
        confidence:        float,
        method:            str,
        sql_expression:    str,
        verdict:           str | None = None,
        reviewer_action:   str | None = None,
        notes:             str = "",
        profile_signature: dict | None = None,
    ) -> str:
        row_id = _uid()
        sig_json = json.dumps(profile_signature, default=str) if profile_signature is not None else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO mapping_history
                    (id, run_id, source_name, table_name, source_column,
                     target_field, confidence, method, sql_expression,
                     verdict, reviewer_action, notes, profile_signature_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row_id, run_id, source_name, table_name, source_column,
                 target_field, confidence, method, sql_expression,
                 verdict, reviewer_action, notes, sig_json, _now()),
            )
            self._conn.commit()
        return row_id

    def update_mapping_verdict(self, run_id: str, source_column: str, verdict: str) -> int:
        """Set verdict on the mapping_history row(s) for this run+column. Returns rows updated."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE mapping_history SET verdict = ?
                WHERE run_id = ? AND source_column = ?
                """,
                (verdict, run_id, source_column),
            )
            self._conn.commit()
            return cur.rowcount

    def update_mapping_review(self, run_id: str, source_column: str, reviewer_action: str) -> int:
        """Set reviewer_action on the mapping_history row(s) for this run+column."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE mapping_history SET reviewer_action = ?
                WHERE run_id = ? AND source_column = ?
                """,
                (reviewer_action, run_id, source_column),
            )
            self._conn.commit()
            return cur.rowcount

    def has_run(self, run_id: str) -> bool:
        """True if any mapping_history row already exists for this run_id.
        Used by backfill.py to make the one-time migration idempotent."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM mapping_history WHERE run_id = ? LIMIT 1", (run_id,)
            )
            return cur.fetchone() is not None

    def get_mapping_history(
        self,
        source_name:   str,
        table_name:    str | None = None,
        source_column: str | None = None,
        limit:         int        = 50,
    ) -> list[dict]:
        query = "SELECT * FROM mapping_history WHERE source_name = ?"
        params: list[Any] = [source_name]
        if table_name:
            query += " AND table_name = ?"
            params.append(table_name)
        if source_column:
            query += " AND source_column = ?"
            params.append(source_column)
        query += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            cur = self._conn.execute(query, params)
            return _rows(cur)

    # ── loss_runs ──────────────────────────────────────────────────────────────

    def record_loss_run(
        self,
        run_id:          str,
        source_name:     str,
        table_name:      str,
        metrics:         dict,
        config_snapshot: dict,
    ) -> str:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO loss_runs
                    (run_id, source_name, table_name, metrics_json,
                     config_snapshot_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, source_name, table_name, json.dumps(metrics, default=str),
                 json.dumps(config_snapshot, default=str), _now()),
            )
            self._conn.commit()
        return run_id

    def get_loss_runs(self, source_name: str, limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM loss_runs WHERE source_name = ?
                ORDER BY recorded_at DESC LIMIT ?
                """,
                (source_name, limit),
            )
            rows = _rows(cur)
        for r in rows:
            r["metrics"] = json.loads(r["metrics_json"])
            r["config_snapshot"] = json.loads(r["config_snapshot_json"])
        return rows

    # ── prompt_versions ────────────────────────────────────────────────────────

    def record_prompt_version(
        self,
        agent_name:        str,
        prompt_text:       str,
        parent_version_id: str | None = None,
        loss_before:       float | None = None,
        loss_after:        float | None = None,
        accepted:          bool = False,
    ) -> str:
        version_id = _uid()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO prompt_versions
                    (version_id, agent_name, prompt_text, parent_version_id,
                     loss_before, loss_after, accepted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (version_id, agent_name, prompt_text, parent_version_id,
                 loss_before, loss_after, int(accepted), _now()),
            )
            self._conn.commit()
        return version_id

    def get_prompt_versions(self, agent_name: str, accepted_only: bool = False) -> list[dict]:
        query = "SELECT * FROM prompt_versions WHERE agent_name = ?"
        params: list[Any] = [agent_name]
        if accepted_only:
            query += " AND accepted = 1"
        query += " ORDER BY created_at DESC"
        with self._lock:
            cur = self._conn.execute(query, params)
            return _rows(cur)

    # ── few_shot_examples (MAP-4 Layer 1) ─────────────────────────────────────

    def add_few_shot_example(
        self,
        source_name:       str,
        source_column:     str,
        target_field:      str | None,
        sql_expression:    str,
        reasoning:         str,
        profile_signature: dict,
        origin:            str,
    ) -> str:
        example_id = _uid()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO few_shot_examples
                    (example_id, source_name, source_column, target_field,
                     sql_expression, reasoning, profile_signature_json,
                     origin, status, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (example_id, source_name, source_column, target_field,
                 sql_expression, reasoning, json.dumps(profile_signature, default=str),
                 origin, _now()),
            )
            self._conn.commit()
        return example_id

    def get_few_shot_examples(self, source_name: str, status: str = "active") -> list[dict]:
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT * FROM few_shot_examples WHERE source_name = ? AND status = ?
                ORDER BY added_at DESC
                """,
                (source_name, status),
            )
            return _rows(cur)

    def has_few_shot_example(self, source_name: str, source_column: str, origin: str) -> bool:
        """True if an active example for this column+origin already exists.
        Used by curate_few_shot_bank.py to avoid duplicate inserts on re-runs."""
        with self._lock:
            cur = self._conn.execute(
                """
                SELECT 1 FROM few_shot_examples
                WHERE source_name = ? AND source_column = ? AND origin = ? AND status = 'active'
                LIMIT 1
                """,
                (source_name, source_column, origin),
            )
            return cur.fetchone() is not None

    def retire_few_shot_example(self, example_id: str, reason: str) -> int:
        """Mark an example retired (e.g. it correlated with worse downstream loss).
        Returns rows updated (0 or 1)."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE few_shot_examples
                SET status = 'retired', retired_at = ?, retire_reason = ?
                WHERE example_id = ? AND status = 'active'
                """,
                (_now(), reason, example_id),
            )
            self._conn.commit()
            return cur.rowcount


# ── Factory ───────────────────────────────────────────────────────────────────

def open_store(db_path: Path | None = None) -> MetamodelStore | None:
    """
    Open the metamodel store. Returns None on any error so callers can treat
    history as optional — the mapping/review/scoring pipeline must keep
    working even if the DB file is missing, locked, or corrupt.
    """
    if db_path is None:
        db_path = default_path()
    try:
        return MetamodelStore(db_path)
    except Exception:
        return None
