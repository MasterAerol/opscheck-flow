"""Local multi-process queue API: identity, leases, fencing, retries, and replay."""
from __future__ import annotations

import math
from pathlib import Path
import secrets
import unicodedata
import uuid

from . import event_store, ingestion, queue_store as store
from .core import OpsCheckError
from .workflow import _state_path

DEFAULT_STATE = ingestion.DEFAULT_STATE


class LeaseLostError(OpsCheckError):
    """The attempt no longer has authority to change delivery state."""


def integer(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise OpsCheckError(f"{name} must be an integer between {low} and {high}.")
    return value


def seconds(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise OpsCheckError(f"{name} must be between {low} and {high} seconds.")
    return float(value)


def worker_identity(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(
            unicodedata.category(char).startswith("C") for char in value):
        raise OpsCheckError("worker_id must be nonblank, at most 128 characters, without control characters.")
    return value


def enqueue(manifest_path, state_dir=DEFAULT_STATE, *, max_attempts: int = 3, priority: int = 0) -> dict:
    integer(max_attempts, "max_attempts", 1, 10)
    integer(priority, "priority", -100, 100)
    event = ingestion.receive(manifest_path, state_dir)
    if event["status"] == "INVALID":
        raise OpsCheckError(f"Invalid event {event['id']}: {event['error']}")
    paths = [Path(p) for p in event["workflow_config"]["input_paths"]] + [Path(event["manifest_path"])]
    with ingestion._database(state_dir, paths, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            run_id = ingestion.reserve(connection, event["id"])
            row = connection.execute("SELECT id FROM queue_jobs WHERE ingestion_id=?", (event["id"],)).fetchone()
            created = row is None
            job_id = "job_" + uuid.uuid4().hex if created else row[0]
            if created:
                timestamp = store.stamp()
                connection.execute("""INSERT INTO queue_jobs
                    (id,ingestion_id,workflow_run_id,status,priority,max_attempts,available_at,created_at,updated_at)
                    VALUES(?,?,?,'QUEUED',?,?,?,?,?)""",
                    (job_id, event["id"], run_id, priority, max_attempts, timestamp, timestamp, timestamp))
                store.log(connection, job_id, "job_created", ingestion_id=event["id"], workflow_run_id=run_id)
            event_store.synchronize(connection, run_id)
            store.synchronize(connection, run_id)
            return {**store.get(connection, job_id), "duplicate": event["duplicate"], "job_created": created}


def scan(inbox, state_dir=DEFAULT_STATE, *, max_attempts: int = 3, priority: int = 0) -> dict:
    integer(max_attempts, "max_attempts", 1, 10)
    integer(priority, "priority", -100, 100)
    directory = Path(inbox).expanduser().absolute()
    if not directory.is_dir():
        raise OpsCheckError("Inbox must be an existing directory.")
    result = {"scanned": 0, "new_queue_jobs": 0, "existing_jobs": 0, "failed": 0, "jobs": []}
    for path in sorted(directory.glob("*.opscheck.json"), key=lambda p: p.name):
        result["scanned"] += 1
        try:
            job = enqueue(path, state_dir, max_attempts=max_attempts, priority=priority)
            result["new_queue_jobs" if job["job_created"] else "existing_jobs"] += 1
            result["jobs"].append(job)
        except (OpsCheckError, OSError, ValueError) as exc:
            result["failed"] += 1
            result["jobs"].append({"manifest_path": str(path), "error": str(exc)})
    return result


def inspect_job(job_id: str, state_dir=DEFAULT_STATE) -> dict:
    with ingestion._database(state_dir, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            event_store.synchronize(connection)
            store.synchronize(connection, expired_before=store.stamp())
            job = store.get(connection, job_id)
            if job is None:
                raise OpsCheckError("Queue job not found.")
            return job


def list_jobs(state_dir=DEFAULT_STATE, *, deadletters: bool = False) -> list[dict]:
    directory = Path(state_dir).expanduser().absolute()
    if not _state_path(directory, [], create=False).exists():
        return []
    with ingestion._database(directory, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            event_store.synchronize(connection)
            store.synchronize(connection, expired_before=store.stamp())
            query = "SELECT id FROM queue_jobs" + (" WHERE status='DEAD_LETTER'" if deadletters else "")
            return [store.get(connection, r[0]) for r in connection.execute(query + " ORDER BY created_at,id")]


def claim(state_dir=DEFAULT_STATE, *, worker_id: str, lease_seconds: float = 30) -> dict | None:
    worker_identity(worker_id)
    seconds(lease_seconds, "lease_seconds", 5, 3600)
    directory = Path(state_dir).expanduser().absolute()
    if not _state_path(directory, [], create=False).exists():
        return None
    with ingestion._database(directory, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            now = store.clock()
            timestamp = store.stamp(now)
            event_store.synchronize(connection)
            # Reconcile successful checkpoints before charging expired deliveries,
            # using one cutoff even if a lease expires while this transaction runs.
            store.synchronize(connection, expired_before=timestamp)
            expired = connection.execute("SELECT * FROM queue_jobs WHERE status='LEASED' AND lease_expires_at<=?", (timestamp,)).fetchall()
            for job in expired:
                store.failure(connection, job, "Worker lease expired.", "LeaseExpired", expired=True)
            # Expiry recovery writes available_at using the current clock. Refresh
            # the selection boundary so microseconds elapsed during recovery do not
            # force an otherwise eligible reclaimed job to wait for another poll.
            now = store.clock()
            timestamp = store.stamp(now)
            job = connection.execute("""SELECT * FROM queue_jobs WHERE status='QUEUED' AND available_at<=?
                AND budget_attempts<max_attempts ORDER BY priority DESC,available_at,created_at,id LIMIT 1""", (timestamp,)).fetchone()
            if job is None:
                return None
            token = secrets.token_hex(32)
            connection.execute("""UPDATE queue_jobs SET status='LEASED',lease_owner=?,lease_token=?,lease_expires_at=?,
                attempts=attempts+1,budget_attempts=budget_attempts+1,started_at=COALESCE(started_at,?),updated_at=? WHERE id=?""",
                (worker_id, token, store.stamp(now + lease_seconds), timestamp, timestamp, job["id"]))
            connection.execute("""INSERT INTO queue_attempts
                (job_id,attempt_number,replay_number,worker_id,lease_token,started_at,heartbeat_at) VALUES(?,?,?,?,?,?,?)""",
                (job["id"], job["attempts"] + 1, job["replay_count"], worker_id, token, timestamp, timestamp))
            store.log(connection, job["id"], "job_claimed", worker_id=worker_id, attempt=job["attempts"] + 1)
            return store.get(connection, job["id"], private=True)


def _owned(connection, job_id: str, token: str):
    return connection.execute("""SELECT * FROM queue_jobs WHERE id=? AND lease_token=?
        AND status='LEASED' AND lease_expires_at>?""", (job_id, token, store.stamp())).fetchone()


def _lost(connection, job_id: str, token: str) -> None:
    changed = connection.execute("UPDATE queue_attempts SET lease_lost=1 WHERE job_id=? AND lease_token=? AND lease_lost=0",
                                 (job_id, token))
    if changed.rowcount:
        store.log(connection, job_id, "lease_lost", reason="Attempt no longer owns an unexpired lease.")


def heartbeat(job_id: str, token: str, state_dir=DEFAULT_STATE, *, lease_seconds: float = 30) -> None:
    seconds(lease_seconds, "lease_seconds", 5, 3600)
    with ingestion._database(state_dir, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            owned = _owned(connection, job_id, token)
            if owned:
                timestamp = store.stamp()
                expiry = max(owned["lease_expires_at"], store.stamp(store.clock() + lease_seconds))
                connection.execute("""UPDATE queue_jobs SET lease_expires_at=?,updated_at=?
                    WHERE id=? AND lease_token=? AND status='LEASED'""", (expiry, timestamp, job_id, token))
                connection.execute("UPDATE queue_attempts SET heartbeat_at=? WHERE job_id=? AND lease_token=?", (timestamp, job_id, token))
            else:
                _lost(connection, job_id, token)
        if not owned:
            raise LeaseLostError("Lease lost; heartbeat rejected.")


def finish(job_id: str, token: str, state_dir=DEFAULT_STATE, *, error: str | None = None,
           error_type: str = "DeliveryFailure", terminal: bool = False, busy: bool = False) -> dict:
    """CAS settlement. Successful state is read from the workflow, never the worker."""
    with ingestion._database(state_dir, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            job = _owned(connection, job_id, token)
            if job is None:
                _lost(connection, job_id, token)
            else:
                event_store.synchronize(connection, job["workflow_run_id"])
                run = connection.execute("SELECT status,failure_reason FROM runs WHERE run_id=?", (job["workflow_run_id"],)).fetchone()
                if run and run["status"] in ("WAITING_FOR_APPROVAL", "SUCCEEDED"):
                    store.settle(connection, job, run["status"])
                    if error:
                        store.log(connection, job_id, "delivery_warning", error=str(error)[:4096], error_type=error_type)
                else:
                    revision_limit = connection.execute("SELECT 1 FROM events WHERE run_id=? AND event='revision_limit_reached'",
                                                        (job["workflow_run_id"],)).fetchone() is not None
                    store.failure(connection, job, error or (run["failure_reason"] if run else None) or "Workflow did not reach a checkpoint.",
                                  "RevisionLimit" if revision_limit else error_type, terminal=terminal or revision_limit, busy=busy)
        if job is None:
            raise LeaseLostError("Lease lost; delivery result rejected.")
        return store.get(connection, job_id)


def replay(job_id: str, state_dir=DEFAULT_STATE, *, max_attempts: int | None = None) -> dict:
    if max_attempts is not None:
        integer(max_attempts, "max_attempts", 1, 10)
    with ingestion._database(state_dir, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            event_store.synchronize(connection)
            store.synchronize(connection, expired_before=store.stamp())
            job = store.get(connection, job_id)
            if job is None:
                raise OpsCheckError("Queue job not found.")
            if job["status"] != "DEAD_LETTER" or not job["replayable"]:
                raise OpsCheckError("Only replayable DEAD_LETTER jobs can be replayed; terminal identity/revision failures cannot be reset.")
            connection.execute("""UPDATE queue_jobs SET status='QUEUED',budget_attempts=0,max_attempts=?,available_at=?,
                updated_at=?,dead_lettered_at=NULL,last_error=NULL,last_error_type=NULL,replay_count=replay_count+1 WHERE id=?""",
                (max_attempts or job["max_attempts"], store.stamp(), store.stamp(), job_id))
            store.log(connection, job_id, "job_replayed", replay_count=job["replay_count"] + 1, max_attempts=max_attempts or job["max_attempts"])
            return store.get(connection, job_id)
