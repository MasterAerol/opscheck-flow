"""Durable local inbox ingestion, atomic deduplication, and reserved-run recovery."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import uuid

from . import event_store as store
from .core import OpsCheckError
from .manifests import (MAX_MANIFEST_BYTES, SOURCE_FIELDS, Manifest, candidate_sources,
                        read_document, validate_manifest)
from .workflow import (_connect, _json, _now, _run_lock, _state_path, RunBusyError,
                       run_workflow)

DEFAULT_STATE = Path(".opscheck/runs")


@contextmanager
def _database(state_dir: Path, sources=(), create: bool = True):
    directory = Path(state_dir).expanduser().absolute()
    try:
        database = _state_path(directory, list(sources), create=create)
        if not database.exists() and not create:
            raise OpsCheckError("Event not found.")
        connection = _connect(database)
        try:
            yield connection, directory
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise OpsCheckError(f"Cannot access ingestion state: {exc}") from exc


def _invalid(connection: sqlite3.Connection, path: Path, document: dict | None,
             reason: str, raw_hash: str, fingerprint: str | None = None) -> str:
    identity = "invalid:" + hashlib.sha256(_json([str(path), raw_hash, reason, fingerprint]).encode("utf-8")).hexdigest()
    existing = connection.execute("SELECT id FROM ingestion_events WHERE identity_key=?", (identity,)).fetchone()
    if existing:
        connection.execute("UPDATE ingestion_events SET duplicate_count=duplicate_count+1,updated_at=? WHERE id=?", (_now(), existing[0]))
        store.log(connection, existing[0], "event_duplicate", manifest_path=str(path), invalid=True)
        return existing[0]
    event_id = "evt_" + uuid.uuid4().hex
    timestamp = _now()
    # Retain only recognized fields; unsupported configuration values are not useful
    # diagnostic data and may include secrets. Bad JSON is represented by its hash.
    safe = {name: value for name, value in (document or {}).items()
            if name in {"event_type", "event_id", "key", *SOURCE_FIELDS}}
    external = safe.get("event_id") if isinstance(safe.get("event_id"), str) else None
    event_type = safe.get("event_type") if isinstance(safe.get("event_type"), str) else None
    connection.execute("""INSERT INTO ingestion_events
        (id,identity_key,event_id,event_type,manifest_path,manifest_json,fingerprint,status,error,received_at,updated_at)
        VALUES(?,?,?,?,?,?,?,'INVALID',?,?,?)""",
        (event_id, identity, external, event_type, str(path), _json(safe) if document is not None else None,
         fingerprint, reason, timestamp, timestamp))
    store.log(connection, event_id, "event_received", manifest_path=str(path), raw_hash=raw_hash)
    store.log(connection, event_id, "event_invalid", error=reason)
    return event_id


def _persist(connection: sqlite3.Connection, manifest: Manifest) -> tuple[str, bool]:
    """Resolve canonical content and external identity in one serialized transaction."""
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        external = manifest.document.get("event_id")
        if external is not None:
            alias = connection.execute("SELECT fingerprint FROM external_event_ids WHERE event_id=?", (external,)).fetchone()
            if alias and alias[0] != manifest.fingerprint:
                reason = f"Event ID {external!r} was already used with different content."
                return _invalid(connection, manifest.path, manifest.document, reason, manifest.raw_hash, manifest.fingerprint), False
        existing = connection.execute("SELECT id FROM ingestion_events WHERE identity_key=?",
                                      ("content:" + manifest.fingerprint,)).fetchone()
        duplicate = existing is not None
        event_id = existing[0] if duplicate else "evt_" + uuid.uuid4().hex
        if duplicate:
            connection.execute("UPDATE ingestion_events SET duplicate_count=duplicate_count+1,updated_at=? WHERE id=?", (_now(), event_id))
            store.log(connection, event_id, "event_duplicate", manifest_path=str(manifest.path), external_event_id=external)
        else:
            timestamp = _now()
            connection.execute("""INSERT INTO ingestion_events
                (id,identity_key,event_id,event_type,manifest_path,manifest_json,fingerprint,
                 expected_workflow_fingerprint,workflow_config_json,status,received_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,'RECEIVED',?,?)""",
                (event_id, "content:" + manifest.fingerprint, external, "orders_check", str(manifest.path),
                 _json(manifest.document), manifest.fingerprint, manifest.workflow_fingerprint,
                 _json(manifest.workflow_config), timestamp, timestamp))
            store.log(connection, event_id, "event_received", manifest_path=str(manifest.path), raw_hash=manifest.raw_hash)
            connection.execute("UPDATE ingestion_events SET status='VALIDATED' WHERE id=?", (event_id,))
            store.log(connection, event_id, "event_validated", fingerprint=manifest.fingerprint)
        if external is not None:
            connection.execute("INSERT OR IGNORE INTO external_event_ids VALUES(?,?,?)", (external, event_id, manifest.fingerprint))
        return event_id, duplicate


def reserve(connection: sqlite3.Connection, event_id: str) -> str:
    """Reserve once inside the caller's write transaction (enqueue or ingest claim)."""
    run_id = connection.execute("SELECT workflow_run_id FROM ingestion_events WHERE id=?", (event_id,)).fetchone()[0]
    if run_id is None:
        run_id = uuid.uuid4().hex
        connection.execute("UPDATE ingestion_events SET workflow_run_id=? WHERE id=?", (run_id, event_id))
        store.log(connection, event_id, "workflow_reserved", run_id=run_id)
    return run_id


def _claim(connection: sqlite3.Connection, event: dict, retry: bool) -> str:
    """Reserve the run ID in the same CAS transaction as the durable claim.

    Caller owns the event OS lock. Its release establishes that a saved claim can
    be recovered, without a timeout that could steal a slow live process's claim.
    """
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        timestamp = _now()
        changed = connection.execute("""UPDATE ingestion_events SET status='CLAIMED',
            claimed_at=?,updated_at=?,claim_attempts=claim_attempts+1,error=NULL,retryable=0
            WHERE id=? AND status=? AND claim_attempts=?""",
            (timestamp, timestamp, event["id"], event["status"], event["claim_attempts"]))
        if changed.rowcount != 1:
            raise RunBusyError("Event state changed while claiming; inspect its saved state.")
        if retry:
            store.log(connection, event["id"], "event_retried", run_id=event["workflow_run_id"])
        store.log(connection, event["id"], "event_claimed", attempt=event["claim_attempts"] + 1,
                  recovered=event["workflow_run_id"] is not None)
        return reserve(connection, event["id"])


def _execute(event: dict, run_id: str, directory: Path) -> dict:
    """Reuse the existing engine and its fingerprint checks; never duplicate task logic."""
    config = event["workflow_config"]
    with _database(directory, [Path(path) for path in config["input_paths"]], create=False) as (connection, _):
        existing = connection.execute("SELECT fingerprint FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if existing is not None and existing[0] != event["expected_workflow_fingerprint"]:
            raise OpsCheckError("Reserved workflow fingerprint differs from the received event.")
        briefing_saved = connection.execute("""SELECT 1 FROM tasks WHERE run_id=?
            AND task_id='briefing_agent' AND status='succeeded'""", (run_id,)).fetchone() is not None
    if briefing_saved:
        # Milestone 2 recovery uses the verified snapshot after this boundary,
        # including when original source files have moved since the initial run.
        from .approvals import resume_run
        return resume_run(run_id, directory)
    return run_workflow(*config["input_paths"], key=config["key"], state_dir=directory,
                        reserved_run_id=run_id, expected_fingerprint=event["expected_workflow_fingerprint"],
                        max_human_revisions=config["max_human_revisions"])


def _process(event_id: str, directory: Path, retry: bool = False, *, delivery: bool = False) -> tuple[dict, bool]:
    with _database(directory, create=False) as (connection, directory):
        event = store.get(connection, event_id)
        if event is None:
            raise OpsCheckError("Event not found.")
        paths = [Path(path) for path in (event["workflow_config"] or {}).get("input_paths", [])]
        paths.append(Path(event["manifest_path"]))
        _state_path(directory, paths, create=False)
        created = False
        processing_error = None
        try:
            with _run_lock(directory, event_id, paths):
                with connection:
                    store.synchronize(connection)
                event = store.get(connection, event_id)
                if event["status"] in ("INVALID", "WAITING_FOR_APPROVAL", "COMPLETED") or (
                        event["status"] == "FAILED" and (not retry or not event["retryable"])):
                    if retry and not delivery:
                        raise OpsCheckError(f"Event in {event['status']} is not retryable.")
                    return event, False
                run_id = _claim(connection, event, retry)
                existed = connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is not None
                try:
                    _execute(event, run_id, directory)
                except RunBusyError:
                    if delivery:
                        raise
                    # A direct workflow resume may already own the run lock.
                    pass
                except (OpsCheckError, OSError, ValueError) as exc:
                    with connection:
                        reason = f"{type(exc).__name__}: {exc}"
                        processing_error = reason
                        connection.execute("UPDATE ingestion_events SET status='FAILED',error=?,retryable=1,updated_at=? WHERE id=?",
                                           (reason, _now(), event_id))
                        store.log(connection, event_id, "event_failed", error=reason, run_id=run_id)
                with connection:
                    store.synchronize(connection, run_id)
                created = not existed and connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is not None
        except RunBusyError:
            if delivery:
                raise
            # A duplicate returns the durable reservation/state; it never spins or
            # schedules a second run while the canonical processor is still active.
            pass
        result = store.get(connection, event_id)
        if processing_error:
            # Workflow status remains authoritative even if publishing artifacts
            # failed after its checkpoint committed. Do not hide the CLI failure.
            result["processing_error"] = processing_error
        return result, created


def _invalid_raw_hash(path: Path) -> str:
    try:
        if path.is_file():
            with path.open("rb") as stream:
                return hashlib.sha256(stream.read(MAX_MANIFEST_BYTES + 1)).hexdigest()
    except OSError:
        pass
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def receive(manifest_path, state_dir=DEFAULT_STATE) -> dict:
    """Validate and persist a canonical event without executing its workflow."""
    path = Path(manifest_path).expanduser().absolute()
    document = None
    sources = [path]
    try:
        document, raw_hash = read_document(path)
        sources.extend(candidate_sources(document, path.parent))
        manifest = validate_manifest(path, document, raw_hash)
        sources.extend(manifest.paths)
    except (OpsCheckError, OSError, ValueError, RecursionError) as exc:
        with _database(state_dir, sources) as (connection, _):
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                event_id = _invalid(connection, path, document, str(exc), _invalid_raw_hash(path))
            return {**store.get(connection, event_id), "duplicate": False, "workflow_created": False}
    with _database(state_dir, sources) as (connection, directory):
        event_id, duplicate = _persist(connection, manifest)
        return {**store.get(connection, event_id), "duplicate": duplicate, "workflow_created": False}


def ingest(manifest_path, state_dir=DEFAULT_STATE) -> dict:
    """Receive one submission and synchronously process its canonical workflow."""
    received = receive(manifest_path, state_dir)
    if received["status"] == "INVALID":
        return received
    event, created = _process(received["id"], Path(state_dir))
    return {**event, "duplicate": received["duplicate"], "workflow_created": created}


def inspect_event(event_id: str, state_dir=DEFAULT_STATE) -> dict:
    with _database(state_dir, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            store.synchronize(connection)
            event = store.get(connection, event_id)
            if event is None:
                raise OpsCheckError("Event not found.")
            return event


def list_events(state_dir=DEFAULT_STATE) -> list[dict]:
    directory = Path(state_dir).expanduser().absolute()
    if not _state_path(directory, [], create=False).exists():
        return []
    with _database(directory, create=False) as (connection, _):
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            store.synchronize(connection)
            return [store.get(connection, row[0]) for row in connection.execute(
                "SELECT id FROM ingestion_events ORDER BY received_at DESC,id")]


def retry_event(event_id: str, state_dir=DEFAULT_STATE) -> dict:
    """Retry a failure or recover an interrupted claim using its immutable reservation."""
    event, created = _process(event_id, Path(state_dir), retry=True)
    return {**event, "duplicate": False, "workflow_created": created}


def scan(inbox, state_dir=DEFAULT_STATE) -> dict:
    """Process *.opscheck.json in deterministic order, isolating failures per manifest."""
    directory = Path(inbox).expanduser().absolute()
    if not directory.is_dir():
        raise OpsCheckError("Inbox must be an existing directory.")
    files = sorted(directory.glob("*.opscheck.json"), key=lambda path: path.name)
    summary = {"scanned": len(files), "new_events": 0, "duplicates": 0, "invalid": 0,
               "failed": 0, "workflows_created": 0, "events": []}
    for path in files:
        try:
            event = ingest(path, state_dir)
        except (OpsCheckError, OSError, ValueError) as exc:
            summary["failed"] += 1
            summary["events"].append({"manifest_path": str(path), "error": str(exc), "status": "FAILED"})
            continue
        summary["events"].append(event)
        summary["workflows_created"] += int(event["workflow_created"])
        if event["status"] == "INVALID":
            summary["invalid"] += 1
        elif event["duplicate"]:
            summary["duplicates"] += 1
        else:
            summary["new_events"] += 1
        summary["failed"] += int(event["status"] == "FAILED" or bool(event.get("processing_error")))
    return summary
