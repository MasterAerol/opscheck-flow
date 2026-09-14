"""Standard-library local workers with renewable delivery leases and run locks."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import threading
import time
import uuid

from . import event_store, ingestion, queue, queue_store as store
from .core import OpsCheckError
from .workflow import RunBusyError, _publish_result, _run_lock


class TerminalDeliveryError(OpsCheckError):
    """Immutable identity or unsupported saved execution configuration."""


class Heartbeat:
    def __init__(self, job: dict, state_dir, lease_seconds: float):
        self.job, self.state_dir, self.lease_seconds = job, state_dir, lease_seconds
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._loop, name="opscheck-heartbeat", daemon=False)

    def _loop(self):
        while not self.stop.wait(self.lease_seconds / 3):
            try:
                queue.heartbeat(self.job["id"], self.job["lease_token"], self.state_dir, lease_seconds=self.lease_seconds)
            except Exception as exc:
                # A failed renewal cannot be assumed successful. The workflow may
                # finish under its OS lock, but this worker cannot settle delivery.
                self.error = str(exc)
                self.lost.set()
                return

    def __enter__(self):
        queue.heartbeat(self.job["id"], self.job["lease_token"], self.state_dir, lease_seconds=self.lease_seconds)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()


def _execute(job: dict, state_dir) -> dict:
    """Use Milestone 3's orchestration, including approval-aware recovery."""
    with ingestion._database(state_dir, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            if queue._owned(connection, job["id"], job["lease_token"]) is None:
                raise queue.LeaseLostError("Lease lost before execution.")
            event = event_store.get(connection, job["ingestion_id"])
            run = connection.execute("SELECT fingerprint FROM runs WHERE run_id=?", (job["workflow_run_id"],)).fetchone()
            if (event is None or event["workflow_run_id"] != job["workflow_run_id"] or event["status"] == "INVALID"
                    or not event["workflow_config"] or event["workflow_config"].get("workflow_version") != 2
                    or (run and run[0] != event["expected_workflow_fingerprint"])):
                raise TerminalDeliveryError("Invalid immutable event/run identity or unsupported workflow version.")
            store.log(connection, job["id"], "job_started", attempt=job["attempts"])
    return ingestion._process(job["ingestion_id"], Path(state_dir), retry=True, delivery=True)[0]


def _refresh_report(job: dict, state_dir) -> None:
    """Republish queue metadata under the existing run lock without executing tasks."""
    with ingestion._database(state_dir, create=False) as (connection, directory):
        event = event_store.get(connection, job["ingestion_id"])
        if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (job["workflow_run_id"],)).fetchone() is None:
            return
        paths = [Path(p) for p in event["workflow_config"]["input_paths"]]
        with _run_lock(directory, job["workflow_run_id"], paths):
            _publish_result(connection, job["workflow_run_id"], directory, paths)


def process(job: dict, state_dir=queue.DEFAULT_STATE, *, lease_seconds: float = 30,
            fail_job: bool = False, fail_job_once: bool = False) -> dict:
    error, error_type, terminal, busy = None, "DeliveryFailure", False, False
    heartbeat = Heartbeat(job, state_dir, lease_seconds)
    try:
        with heartbeat:
            if fail_job or (fail_job_once and job["budget_attempts"] == 1 and job["replay_count"] == 0):
                raise OpsCheckError("Demo-only injected delivery failure before workflow execution.")
            event = _execute(job, state_dir)
            error = event.get("processing_error") or (event.get("error") if event["status"] == "FAILED" else None)
    except queue.LeaseLostError:
        raise
    except KeyboardInterrupt:
        try:
            queue.finish(job["id"], job["lease_token"], state_dir, error="Worker interrupted.", error_type="WorkerInterrupted")
        except queue.LeaseLostError:
            pass
        raise
    except Exception as exc:
        error, error_type = str(exc), type(exc).__name__
        terminal, busy = isinstance(exc, TerminalDeliveryError), isinstance(exc, RunBusyError)
    if heartbeat.lost.is_set():
        raise queue.LeaseLostError(f"Heartbeat failed; delivery settlement refused: {heartbeat.error}")
    result = queue.finish(job["id"], job["lease_token"], state_dir, error=error,
                          error_type=error_type, terminal=terminal, busy=busy)
    # Busy execution belongs to another live process; do not compete for its report.
    if not busy:
        try:
            _refresh_report(result, state_dir)
        except RunBusyError:
            pass
        except (OpsCheckError, OSError, ValueError) as exc:
            error = error or f"Queue state saved, but report refresh failed: {exc}"
    return {**result, "processing_error": error, "deferred": busy}


def run(state_dir=queue.DEFAULT_STATE, *, once: bool = False, worker_id: str | None = None,
        poll_interval: float = 2, lease_seconds: float = 30, max_jobs: int | None = None,
        fail_job: bool = False, fail_job_once: bool = False, emit=None) -> int:
    """max_jobs counts successful claims, including failed or deferred deliveries."""
    worker_id = queue.worker_identity(worker_id if worker_id is not None else
                                      f"{socket.gethostname()[:60]}-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    queue.seconds(poll_interval, "poll_interval", .1, 60)
    queue.seconds(lease_seconds, "lease_seconds", 5, 3600)
    if max_jobs is not None:
        queue.integer(max_jobs, "max_jobs", 1, 1000000)
    count, failed = 0, False
    while True:
        job = queue.claim(state_dir, worker_id=worker_id, lease_seconds=lease_seconds)
        if job:
            count += 1
            try:
                result = process(job, state_dir, lease_seconds=lease_seconds, fail_job=fail_job, fail_job_once=fail_job_once)
            except queue.LeaseLostError as exc:
                result = {"id": job["id"], "status": "LEASE_LOST", "processing_error": str(exc)}
            failed = failed or bool(result.get("processing_error") and not result.get("deferred")) or result["status"] == "DEAD_LETTER"
            if emit:
                emit(result)
        elif once and emit:
            emit({"status": "EMPTY"})
        if once or (max_jobs is not None and count >= max_jobs):
            return 2 if failed else 0
        if not job:
            time.sleep(poll_interval)
