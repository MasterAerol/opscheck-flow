"""SQLite event identities, durable run reservations, and ingestion audit history.

Functions accept an owned connection; callers delimit transactions and close it.
Workflow state is authoritative once a reserved run exists.
"""
from __future__ import annotations

import json
import sqlite3

from .workflow import _json, _now

STATES = ("RECEIVED", "VALIDATED", "CLAIMED", "WORKFLOW_CREATED", "WAITING_FOR_APPROVAL",
          "COMPLETED", "FAILED", "INVALID")


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS ingestion_events (
            id TEXT PRIMARY KEY,
            identity_key TEXT NOT NULL UNIQUE,
            event_id TEXT,
            event_type TEXT,
            manifest_path TEXT NOT NULL,
            manifest_json TEXT,
            fingerprint TEXT,
            expected_workflow_fingerprint TEXT,
            workflow_config_json TEXT,
            status TEXT NOT NULL CHECK(status IN ('RECEIVED','VALIDATED','CLAIMED','WORKFLOW_CREATED',
                'WAITING_FOR_APPROVAL','COMPLETED','FAILED','INVALID')),
            workflow_run_id TEXT UNIQUE,
            error TEXT,
            retryable INTEGER NOT NULL DEFAULT 0,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            claim_attempts INTEGER NOT NULL DEFAULT 0,
            received_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claimed_at TEXT,
            workflow_created_at TEXT,
            completed_at TEXT,
            CHECK(status<>'INVALID' OR workflow_run_id IS NULL)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS canonical_event_fingerprint
            ON ingestion_events(fingerprint) WHERE status<>'INVALID';
        CREATE TABLE IF NOT EXISTS external_event_ids (
            event_id TEXT PRIMARY KEY,
            ingestion_id TEXT NOT NULL REFERENCES ingestion_events(id),
            fingerprint TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ingestion_event_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingestion_id TEXT NOT NULL REFERENCES ingestion_events(id),
            event TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            detail_json TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS immutable_event_identity BEFORE UPDATE ON ingestion_events
            WHEN NEW.identity_key<>OLD.identity_key OR NEW.fingerprint IS NOT OLD.fingerprint
                OR NEW.expected_workflow_fingerprint IS NOT OLD.expected_workflow_fingerprint
                OR NEW.workflow_config_json IS NOT OLD.workflow_config_json
                OR (OLD.workflow_run_id IS NOT NULL AND NEW.workflow_run_id IS NOT OLD.workflow_run_id)
            BEGIN SELECT RAISE(ABORT, 'Event identity and run reservation are immutable.'); END;
    """)


def log(connection: sqlite3.Connection, event_id: str, event: str, **detail: object) -> None:
    connection.execute("INSERT INTO ingestion_event_log(ingestion_id,event,timestamp,detail_json) VALUES(?,?,?,?)",
                       (event_id, event, _now(), _json(detail)))


def synchronize(connection: sqlite3.Connection, run_id: str | None = None) -> None:
    """Derive linked event status in the caller's transaction, logging transitions once."""
    if not connection.in_transaction:
        # Lock before reading the old state: a deferred read/update sequence could
        # publish stale state or duplicate a transition observed by another caller.
        connection.execute("BEGIN IMMEDIATE")
    query = """SELECT e.*,r.status AS run_status,r.failure_reason FROM ingestion_events e
               JOIN runs r ON r.run_id=e.workflow_run_id"""
    params = ()
    if run_id is not None:
        query += " WHERE r.run_id=?"
        params = (run_id,)
    for row in connection.execute(query, params).fetchall():
        timestamp = _now()
        if row["workflow_created_at"] is None:
            connection.execute("UPDATE ingestion_events SET workflow_created_at=? WHERE id=?", (timestamp, row["id"]))
            log(connection, row["id"], "workflow_created", run_id=row["workflow_run_id"])
        status = {"WAITING_FOR_APPROVAL": "WAITING_FOR_APPROVAL", "SUCCEEDED": "COMPLETED",
                  "FAILED": "FAILED"}.get(row["run_status"], "WORKFLOW_CREATED")
        error = row["failure_reason"] if status == "FAILED" else None
        if row["status"] == "FAILED" and row["run_status"] in ("PENDING", "RUNNING"):
            # Ingestion can fail to resume an interrupted run (e.g. changed input
            # bytes). Preserve that failure until an explicit retry claims it.
            status, error = "FAILED", row["error"]
        terminal = status == "FAILED" and connection.execute(
            "SELECT 1 FROM events WHERE run_id=? AND event='revision_limit_reached'", (row["workflow_run_id"],)).fetchone()
        retryable = int(status == "FAILED" and not terminal)
        if (status, error, retryable) != (row["status"], row["error"], row["retryable"]):
            connection.execute("""UPDATE ingestion_events SET status=?,error=?,retryable=?,updated_at=?,
                completed_at=? WHERE id=?""", (status, error, retryable, timestamp,
                    (row["completed_at"] or timestamp) if status == "COMPLETED" else None, row["id"]))
            if status != row["status"] or error != row["error"]:
                log(connection, row["id"], {"WAITING_FOR_APPROVAL": "event_waiting_for_approval",
                    "COMPLETED": "event_completed", "FAILED": "event_failed"}.get(status, "event_processing"),
                    run_id=row["workflow_run_id"], status=status, error=error)


def get(connection: sqlite3.Connection, event_id: str) -> dict | None:
    row = connection.execute("""SELECT e.*,r.status AS workflow_status FROM ingestion_events e
        LEFT JOIN runs r ON r.run_id=e.workflow_run_id WHERE e.id=?""", (event_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["manifest"] = json.loads(result.pop("manifest_json")) if row["manifest_json"] is not None else None
    result["workflow_config"] = json.loads(result.pop("workflow_config_json")) if row["workflow_config_json"] is not None else None
    result["external_event_ids"] = [item[0] for item in connection.execute(
        "SELECT event_id FROM external_event_ids WHERE ingestion_id=? ORDER BY event_id", (event_id,))]
    result["history"] = [{"id": item["id"], "event": item["event"], "timestamp": item["timestamp"],
                          **json.loads(item["detail_json"])} for item in connection.execute(
                              "SELECT * FROM ingestion_event_log WHERE ingestion_id=? ORDER BY id", (event_id,))]
    result["retryable"] = bool(result["retryable"])
    return result


def run_metadata(connection: sqlite3.Connection, run_id: str) -> dict | None:
    row = connection.execute("""SELECT id,event_id,event_type,manifest_path,fingerprint,status
        FROM ingestion_events WHERE workflow_run_id=?""", (run_id,)).fetchone()
    return dict(row) if row else None
