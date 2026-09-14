"""Durable human decisions, immutable briefing versions, and bounded revision recovery.

Only the orchestration process writes SQLite. Decisions use compare-and-set in a
write transaction; the OS run lock also protects revision work and report output.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .core import OpsCheckError
from .workflow import (_connect, _event, _json, _now, _publish_result, _result,
                       _run_lock, _state_path, _task_outputs)
from .revision import revise_briefing


def initialize(connection: sqlite3.Connection) -> None:
    """Add approval storage to existing databases without rewriting task evidence."""
    # Serialize schema inspection and migration across initializing processes.
    connection.execute("BEGIN IMMEDIATE")
    with connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        if "failure_reason" not in columns:
            connection.execute("ALTER TABLE runs ADD COLUMN failure_reason TEXT")
        connection.execute("UPDATE runs SET status=upper(status) WHERE status<>upper(status)")
        connection.execute("""CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
            task_name TEXT NOT NULL DEFAULT 'approval_gate', iteration INTEGER NOT NULL CHECK(iteration>0),
            status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
            decision TEXT, reviewer TEXT, comment TEXT, created_at TEXT NOT NULL, decided_at TEXT,
            briefing_reference TEXT NOT NULL, briefing_json TEXT NOT NULL,
            UNIQUE(run_id, iteration),
            CHECK((status='pending' AND decision IS NULL AND decided_at IS NULL) OR
                  (status IN ('approved','rejected') AND decision=status AND decided_at IS NOT NULL))
        )""")
        connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_pending_approval
            ON approvals(run_id) WHERE status='pending'""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS immutable_approval_decision
            BEFORE UPDATE ON approvals WHEN OLD.status<>'pending'
            BEGIN SELECT RAISE(ABORT, 'Approval is already resolved.'); END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS immutable_approval_briefing
            BEFORE UPDATE ON approvals WHEN NEW.briefing_json<>OLD.briefing_json
                OR NEW.id<>OLD.id OR NEW.run_id<>OLD.run_id OR NEW.iteration<>OLD.iteration
                OR NEW.briefing_reference<>OLD.briefing_reference OR NEW.created_at<>OLD.created_at
                OR NEW.task_name<>OLD.task_name
            BEGIN SELECT RAISE(ABORT, 'Approval briefing is immutable.'); END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS retain_approval_history
            BEFORE DELETE ON approvals
            BEGIN SELECT RAISE(ABORT, 'Approval history must be retained.'); END""")


def history(connection: sqlite3.Connection, run_id: str) -> list[dict]:
    """Read ordered decisions and their exact briefing snapshots from SQLite."""
    records = []
    for row in connection.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY iteration", (run_id,)):
        item = dict(row)
        item["briefing"] = json.loads(item.pop("briefing_json"))
        records.append(item)
    return records


def _request(connection: sqlite3.Connection, run_id: str, state_dir: Path,
             briefing: dict, iteration: int) -> None:
    approval_id = uuid.uuid4().hex
    with connection:
        connection.execute("""INSERT INTO approvals
            (id,run_id,iteration,status,created_at,briefing_reference,briefing_json)
            VALUES(?,?,?,'pending',?,?,?)""",
            (approval_id, run_id, iteration, _now(),
             str(state_dir / run_id / f"briefing-v{iteration}.json"), _json(briefing)))
        connection.execute("""INSERT INTO tasks(run_id,task_id,status,attempts)
            VALUES(?,'approval_gate','waiting',?) ON CONFLICT(run_id,task_id)
            DO UPDATE SET status='waiting',attempts=excluded.attempts,error=NULL""", (run_id, iteration))
        connection.execute("UPDATE runs SET status='WAITING_FOR_APPROVAL',updated_at=? WHERE run_id=?", (_now(), run_id))
        _event(connection, run_id, "approval_requested", "approval_gate", approval_id=approval_id, iteration=iteration)
        _event(connection, run_id, "workflow_paused", "approval_gate", approval_id=approval_id, iteration=iteration)


def _revision(connection: sqlite3.Connection, run_id: str, previous: dict, outputs: dict) -> dict | None:
    iteration = previous["iteration"]
    task = f"revision_agent_{iteration}"
    if task in outputs:
        return outputs[task]
    with connection:
        connection.execute("""INSERT INTO tasks(run_id,task_id,status,attempts)
            VALUES(?,?,'running',1) ON CONFLICT(run_id,task_id)
            DO UPDATE SET status='running',attempts=attempts+1,error=NULL""", (run_id, task))
        _event(connection, run_id, "revision_started", task, iteration=iteration,
               approval_id=previous["id"])
    try:
        revised = revise_briefing(previous["briefing"], outputs["verifier"], outputs["quality_agent"],
                                 outputs["change_agent"], previous["comment"], iteration)
        serialized = _json(revised)
    except Exception as exc:
        reason = f"Revision failed: {type(exc).__name__}: {exc}"
        with connection:
            connection.execute("UPDATE tasks SET status='failed',error=? WHERE run_id=? AND task_id=?", (reason, run_id, task))
            connection.execute("UPDATE runs SET status='FAILED',failure_reason=?,updated_at=? WHERE run_id=?", (reason, _now(), run_id))
            _event(connection, run_id, "task_failed", task, error=reason)
            _event(connection, run_id, "run_completed", status="FAILED", reason=reason)
        return None
    with connection:
        connection.execute("UPDATE tasks SET status='succeeded',output_json=? WHERE run_id=? AND task_id=?", (serialized, run_id, task))
        _event(connection, run_id, "revision_completed", task, iteration=iteration, approval_id=previous["id"])
    return revised


def advance(connection: sqlite3.Connection, run_id: str, state_dir: Path, config: dict) -> None:
    """Continue solely from committed evidence; safe after every crash boundary."""
    run = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run["status"] in ("FAILED", "SUCCEEDED", "WAITING_FOR_APPROVAL"):
        return
    outputs = _task_outputs(connection, run_id)
    if "briefing_agent" not in outputs:
        return
    approvals = history(connection, run_id)
    if not approvals:
        _request(connection, run_id, state_dir, outputs["briefing_agent"], 1)
        return
    previous = approvals[-1]
    if previous["status"] == "approved":
        with connection:
            connection.execute("UPDATE tasks SET status='succeeded' WHERE run_id=? AND task_id='approval_gate'", (run_id,))
            connection.execute("UPDATE runs SET status='SUCCEEDED',failure_reason=NULL,updated_at=? WHERE run_id=?", (_now(), run_id))
            _event(connection, run_id, "run_completed", status="SUCCEEDED")
    elif previous["status"] == "rejected":
        rejected = sum(item["status"] == "rejected" for item in approvals)
        if rejected >= config.get("max_human_revisions", 3):
            reason = f"Human approval revision limit reached after {rejected} rejected versions."
            with connection:
                connection.execute("UPDATE runs SET status='FAILED',failure_reason=?,updated_at=? WHERE run_id=?", (reason, _now(), run_id))
                connection.execute("UPDATE tasks SET status='failed',error=? WHERE run_id=? AND task_id='approval_gate'", (reason, run_id))
                _event(connection, run_id, "revision_limit_reached", "approval_gate", iteration=previous["iteration"], reason=reason)
                _event(connection, run_id, "run_completed", status="FAILED", reason=reason)
            return
        revised = _revision(connection, run_id, previous, outputs)
        if revised is not None:
            _request(connection, run_id, state_dir, revised, previous["iteration"] + 1)


@contextmanager
def _saved_run(run_id: str, state_dir: Path):
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None:
        raise OpsCheckError("Run not found.")
    state_dir = Path(state_dir).expanduser().absolute()
    try:
        database = _state_path(state_dir, [], create=False)
        if not database.exists():
            raise OpsCheckError("Run not found.")
        connection = _connect(database)
        try:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise OpsCheckError("Run not found.")
            config = json.loads(row["config_json"])
            paths = [Path(path) for path in config["input_paths"]]
            _state_path(state_dir, paths, create=False)
            yield connection, state_dir, config, paths
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise OpsCheckError(f"Cannot access workflow state: {exc}") from exc


def inspect_run(run_id: str, state_dir=Path(".opscheck/runs")) -> dict:
    """Return a consistent database snapshot without reading reports or source CSVs."""
    with _saved_run(run_id, state_dir) as (connection, directory, _, _):
        from .event_store import synchronize
        connection.execute("BEGIN IMMEDIATE")
        with connection:
            synchronize(connection, run_id)
            return _result(connection, run_id, directory)


def decide(run_id: str, decision: str, reviewer: str | None = None, comment: str | None = None,
           state_dir=Path(".opscheck/runs"), approval_id: str | None = None) -> dict:
    """Resolve the version observed before acquiring the execution lock exactly once.

    Explicit approval IDs protect delayed clients. Without one, snapshot the current
    request before taking the lock, so a racing command cannot decide its successor.
    """
    if decision not in ("approved", "rejected"):
        raise OpsCheckError("Decision must be approved or rejected.")
    if decision == "rejected" and (not isinstance(comment, str) or not comment.strip()):
        raise OpsCheckError("Rejection requires a nonblank comment.")
    if reviewer is not None and (not isinstance(reviewer, str) or not reviewer.strip()):
        raise OpsCheckError("Reviewer must be nonblank when supplied.")
    if comment is not None and not isinstance(comment, str):
        raise OpsCheckError("Comment must be text.")
    with _saved_run(run_id, state_dir) as (connection, directory, config, paths):
        current = connection.execute("SELECT id FROM approvals WHERE run_id=? AND status='pending'", (run_id,)).fetchone()
        expected = approval_id or (current["id"] if current else None)
        with _run_lock(directory, run_id, paths):
            connection.execute("BEGIN IMMEDIATE")
            with connection:
                run = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if run["status"] == "SUCCEEDED":
                    raise OpsCheckError("Run is already complete.")
                if run["status"] == "FAILED":
                    raise OpsCheckError(f"Cannot {'approve' if decision == 'approved' else 'reject'} a failed run.")
                if run["status"] != "WAITING_FOR_APPROVAL":
                    raise OpsCheckError("No pending approval exists for this run.")
                changed = connection.execute("""UPDATE approvals SET status=?,decision=?,reviewer=?,comment=?,decided_at=?
                    WHERE id=? AND run_id=? AND status='pending'""",
                    (decision, decision, reviewer, comment, _now(), expected, run_id))
                if changed.rowcount != 1:
                    raise OpsCheckError("No pending approval exists for this run; the requested approval may already be resolved.")
                iteration = connection.execute("SELECT iteration FROM approvals WHERE id=?", (expected,)).fetchone()[0]
                connection.execute("UPDATE runs SET status='RUNNING',updated_at=? WHERE run_id=?", (_now(), run_id))
                _event(connection, run_id, f"approval_{decision}", "approval_gate",
                       approval_id=expected, iteration=iteration, reviewer=reviewer)
                _event(connection, run_id, "workflow_resumed", "approval_gate", approval_id=expected, iteration=iteration)
            advance(connection, run_id, directory, config)
            try:
                return _publish_result(connection, run_id, directory, paths)
            except OSError as exc:
                raise OpsCheckError("Decision was saved, but reports could not be written. "
                                    f"Fix the filesystem problem and resume run {run_id}: {exc}") from exc


def resume_run(run_id: str, state_dir=Path(".opscheck/runs"), max_attempts: int = 2) -> dict:
    """Resume saved configuration, including a crash after a committed human decision."""
    from .workflow import run_workflow
    if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
        raise OpsCheckError("max_attempts must be an integer between 1 and 5.")
    with _saved_run(run_id, state_dir) as (connection, directory, config, paths):
        if "briefing_agent" in _task_outputs(connection, run_id):
            with _run_lock(directory, run_id, paths):
                # Revision failures can be retried; a rejection-limit failure is terminal.
                terminal = connection.execute("SELECT 1 FROM events WHERE run_id=? AND event='revision_limit_reached'", (run_id,)).fetchone()
                with connection:
                    status = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
                    if status == "FAILED" and not terminal:
                        connection.execute("UPDATE runs SET status='RUNNING',failure_reason=NULL WHERE run_id=?", (run_id,))
                        _event(connection, run_id, "workflow_resumed")
                advance(connection, run_id, directory, config)
                return _publish_result(connection, run_id, directory, paths)
    return run_workflow(*paths, key=config["key"], state_dir=directory, run_id=run_id,
                        max_attempts=max_attempts, llm_url=config["llm_url"], model=config["model"],
                        max_rounds=config["max_rounds"], max_human_revisions=config.get("max_human_revisions", 3))
