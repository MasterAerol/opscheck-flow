"""Durable queue state. Callers own, transact, and explicitly close connections."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time

from .workflow import _json
from .core import OpsCheckError

STATES = ("QUEUED", "LEASED", "WAITING_FOR_APPROVAL", "SUCCEEDED", "DEAD_LETTER")


def clock() -> float:
    """Patchable wall clock; all persisted timestamps use UTC with fixed precision."""
    return time.time()


def stamp(value: float | None = None) -> str:
    return datetime.fromtimestamp(clock() if value is None else value, timezone.utc).isoformat(timespec="microseconds")


def initialize(connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS queue_jobs (
            id TEXT PRIMARY KEY,
            ingestion_id TEXT NOT NULL UNIQUE REFERENCES ingestion_events(id),
            workflow_run_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('QUEUED','LEASED','WAITING_FOR_APPROVAL','SUCCEEDED','DEAD_LETTER')),
            priority INTEGER NOT NULL DEFAULT 0 CHECK(priority BETWEEN -100 AND 100),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
            budget_attempts INTEGER NOT NULL DEFAULT 0 CHECK(budget_attempts>=0),
            max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 10),
            available_at TEXT NOT NULL,
            lease_owner TEXT,
            lease_token TEXT,
            lease_expires_at TEXT,
            last_error TEXT,
            last_error_type TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            dead_lettered_at TEXT,
            replay_count INTEGER NOT NULL DEFAULT 0 CHECK(replay_count>=0),
            replayable INTEGER NOT NULL DEFAULT 1 CHECK(replayable IN (0,1)),
            CHECK((status='LEASED' AND lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
               OR (status<>'LEASED' AND lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL))
        );
        CREATE INDEX IF NOT EXISTS queue_eligible ON queue_jobs(status,available_at,priority);
        CREATE TABLE IF NOT EXISTS queue_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES queue_jobs(id),
            attempt_number INTEGER NOT NULL,
            replay_number INTEGER NOT NULL,
            worker_id TEXT NOT NULL,
            lease_token TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            finished_at TEXT,
            outcome TEXT NOT NULL DEFAULT 'RUNNING',
            error TEXT,
            lease_lost INTEGER NOT NULL DEFAULT 0,
            UNIQUE(job_id,attempt_number)
        );
        CREATE TABLE IF NOT EXISTS queue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES queue_jobs(id),
            event TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            detail_json TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS immutable_queue_identity BEFORE UPDATE ON queue_jobs
            WHEN NEW.id<>OLD.id OR NEW.ingestion_id<>OLD.ingestion_id OR NEW.workflow_run_id<>OLD.workflow_run_id
            BEGIN SELECT RAISE(ABORT, 'Queue identity is immutable.'); END;
        CREATE TRIGGER IF NOT EXISTS queue_matches_reservation BEFORE INSERT ON queue_jobs
            WHEN NOT EXISTS(SELECT 1 FROM ingestion_events WHERE id=NEW.ingestion_id
                AND workflow_run_id=NEW.workflow_run_id AND status<>'INVALID')
            BEGIN SELECT RAISE(ABORT, 'Queue must use the canonical event reservation.'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_queue_attempt BEFORE UPDATE ON queue_attempts
            WHEN NEW.job_id<>OLD.job_id OR NEW.attempt_number<>OLD.attempt_number
                OR NEW.lease_token<>OLD.lease_token OR NEW.worker_id<>OLD.worker_id OR NEW.replay_number<>OLD.replay_number
            BEGIN SELECT RAISE(ABORT, 'Attempt identity is immutable.'); END;
    """)


def log(connection, job_id: str, event: str, **detail) -> None:
    connection.execute("INSERT INTO queue_events(job_id,event,timestamp,detail_json) VALUES(?,?,?,?)",
                       (job_id, event, stamp(), _json(detail)))


def get(connection, job_id: str, *, private: bool = False) -> dict | None:
    row = connection.execute("SELECT * FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["replayable"] = bool(job["replayable"])
    job["history"] = [{"id": r["id"], "event": r["event"], "timestamp": r["timestamp"],
                       **json.loads(r["detail_json"])} for r in connection.execute(
                           "SELECT * FROM queue_events WHERE job_id=? ORDER BY id", (job_id,))]
    job["attempt_history"] = [dict(r) for r in connection.execute(
        "SELECT * FROM queue_attempts WHERE job_id=? ORDER BY attempt_number", (job_id,))]
    if not private:
        job.pop("lease_token")
        for attempt in job["attempt_history"]:
            attempt.pop("lease_token")
    return job


def end_attempt(connection, job, outcome: str, error: str | None = None, *, lost: bool = False) -> None:
    if job["lease_token"]:
        connection.execute("""UPDATE queue_attempts SET outcome=?,error=?,finished_at=?,lease_lost=?
            WHERE job_id=? AND lease_token=? AND finished_at IS NULL""",
            (outcome, error, stamp(), int(lost), job["id"], job["lease_token"]))


def settle(connection, job, status: str) -> None:
    end_attempt(connection, job, status)
    changed = connection.execute("""UPDATE queue_jobs SET status=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
        updated_at=?,completed_at=?,last_error=NULL,last_error_type=NULL
        WHERE id=? AND status=? AND lease_token IS ?""",
        (status, stamp(), (job["completed_at"] or stamp()) if status == "SUCCEEDED" else None,
         job["id"], job["status"], job["lease_token"]))
    if changed.rowcount != 1:
        raise OpsCheckError("Queue ownership changed during settlement.")
    if job["status"] != status:
        log(connection, job["id"], "job_succeeded" if status == "SUCCEEDED" else "job_waiting_for_approval")


def failure(connection, job, error: str, error_type: str, *, terminal: bool = False,
            expired: bool = False, busy: bool = False) -> None:
    """Caller has serialized ownership (token checked, or expired lease in same txn)."""
    error = str(error)[:4096]
    dead = terminal or (not busy and job["budget_attempts"] >= job["max_attempts"])
    outcome = "EXPIRED" if expired else "DEFERRED" if busy else "FAILED"
    end_attempt(connection, job, outcome, error, lost=expired)
    delay = 0 if expired else 1 if busy else min(60, 2 ** max(0, min(10, job["budget_attempts"] - 1)))
    changed = connection.execute("""UPDATE queue_jobs SET status=?,available_at=?,lease_owner=NULL,lease_token=NULL,
        lease_expires_at=NULL,last_error=?,last_error_type=?,updated_at=?,dead_lettered_at=?,replayable=?,
        budget_attempts=budget_attempts-? WHERE id=? AND status=? AND lease_token IS ?""",
        ("DEAD_LETTER" if dead else "QUEUED", stamp(clock() + delay), error, error_type, stamp(),
         stamp() if dead else None, int(not terminal), int(busy), job["id"], job["status"], job["lease_token"]))
    if changed.rowcount != 1:
        raise OpsCheckError("Queue ownership changed during failure handling.")
    log(connection, job["id"], "lease_expired" if expired else "job_deferred" if busy else "job_failed",
        attempt=job["attempts"], error=error, error_type=error_type)
    log(connection, job["id"], "job_dead_lettered" if dead else "job_requeued",
        delay_seconds=delay, replayable=not terminal)


def synchronize(connection, run_id: str | None = None, *, expired_before: str | None = None) -> None:
    """Workflow facts override delivery state; no worker token is accepted here.

    Publishing skips leases; observers may recover only leases expired at their
    captured UTC cutoff. Unexpired ownership remains exclusive even after a saved
    workflow checkpoint. Claiming uses the same cutoff for this reconciliation
    and expiry failure handling, so a checkpoint cannot fall between the two.
    Human decisions on released jobs synchronize directly.
    """
    if not connection.in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    query = """SELECT q.*,r.status AS run_status,r.failure_reason FROM queue_jobs q
        JOIN runs r ON r.run_id=q.workflow_run_id"""
    params = ()
    if run_id is not None:
        query += " WHERE r.run_id=?"
        params = (run_id,)
    for job in connection.execute(query, params).fetchall():
        if job["status"] == "LEASED" and (expired_before is None or job["lease_expires_at"] > expired_before):
            continue
        if job["run_status"] in ("WAITING_FOR_APPROVAL", "SUCCEEDED"):
            if job["status"] != job["run_status"]:
                settle(connection, job, job["run_status"])
        elif job["run_status"] == "FAILED":
            terminal = connection.execute("SELECT 1 FROM events WHERE run_id=? AND event='revision_limit_reached'",
                                          (job["workflow_run_id"],)).fetchone() is not None
            if terminal and (job["status"] != "DEAD_LETTER" or job["replayable"]):
                failure(connection, job, job["failure_reason"] or "Human revision limit reached.",
                        "RevisionLimit", terminal=True)
            elif job["status"] == "WAITING_FOR_APPROVAL":
                failure(connection, job, job["failure_reason"] or "Human revision failed.", "WorkflowFailure")


def run_metadata(connection, run_id: str) -> dict | None:
    row = connection.execute("""SELECT id,status,attempts,budget_attempts,max_attempts,replay_count,last_error
        FROM queue_jobs WHERE workflow_run_id=?""", (run_id,)).fetchone()
    return dict(row) if row else None
