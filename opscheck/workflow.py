"""Persistent, bounded workflows with two parallel specialist tool workers.

The local workers and verifier are deterministic Python roles. Only an explicitly
configured model adapter makes network calls. CSV files are always read-only.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from .core import OpsCheckError, compare, load_csv, load_rules, validate

RUN_STATES = {"PENDING", "RUNNING", "WAITING_FOR_APPROVAL", "SUCCEEDED", "FAILED"}


PLAN = {
    "planner": [],
    "quality_agent": ["planner"],
    "change_agent": ["planner"],
    "verifier": ["quality_agent", "change_agent"],
    "briefing_agent": ["verifier"],
}

WORKFLOW_PLAN = {**PLAN, "approval_gate": ["briefing_agent"],
                 "revision_agent": ["approval_gate:rejected"]}


class TransientTaskError(RuntimeError):
    """A deliberately injected failure used to demonstrate retry and recovery."""

    retryable = True


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _same_file(left: Path, right: Path) -> bool:
    if left.resolve() == right.resolve():
        return True
    try:
        return left.samefile(right)
    except FileNotFoundError:
        # SQLite may remove a committed rollback journal during this check.
        return False


def _protect_state_file(path: Path, sources: list[Path]) -> None:
    # SQLite can write sidecars; reject links even if they are not source files.
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if not stat.S_ISREG(metadata.st_mode):
            raise OpsCheckError(f"State file must be a regular file, not a symlink: {path}")
        if metadata.st_nlink > 1:
            raise OpsCheckError(f"State file must not have hardlinks: {path}")
    if any(_same_file(path, source) for source in sources):
        raise OpsCheckError(f"Workflow state would overwrite an input: {path}")


def _state_path(state_dir: Path, sources: list[Path], create: bool = True) -> Path:
    if state_dir.exists() and not state_dir.is_dir():
        raise OpsCheckError(f"State directory is not a directory: {state_dir}")
    if create:
        state_dir.mkdir(parents=True, exist_ok=True)
    database = state_dir / "runs.sqlite3"
    for suffix in ("", "-journal", "-wal", "-shm"):
        _protect_state_file(Path(str(database) + suffix), sources)
    return database


@contextmanager
def _run_lock(state_dir: Path, run_id: str, sources: list[Path]):
    """OS-owned locks are released on process exit, including a crash.

    The small lock file stays in place: unlinking it could let two processes lock
    different inodes for the same run. Its existence does not mean a run is busy.
    """
    path = state_dir / f"{run_id}.lock"
    _protect_state_file(path, sources)
    with path.open("a+b") as stream:
        if os.name == "posix":
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OpsCheckError(f"Run {run_id} is already executing.") from exc
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise OpsCheckError(f"Run {run_id} is already executing.") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            raise OpsCheckError("Exclusive workflow locks require POSIX or Windows.")


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL,
                config_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                output_json TEXT,
                PRIMARY KEY (run_id, task_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(run_id),
                event TEXT NOT NULL,
                task TEXT,
                timestamp TEXT NOT NULL,
                detail_json TEXT NOT NULL
            );
        """)
        from .approvals import initialize
        initialize(connection)
    except BaseException:
        # Ownership transfers only after initialization succeeds.
        connection.close()
        raise
    return connection


def _event(connection: sqlite3.Connection, run_id: str, event: str,
           task: str | None = None, **detail: object) -> None:
    connection.execute(
        "INSERT INTO events(run_id,event,task,timestamp,detail_json) VALUES(?,?,?,?,?)",
        (run_id, event, task, _now(), _json(detail)),
    )


def _fingerprint(paths: list[Path], config: dict) -> str:
    contents = []
    for path in paths:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        contents.append({"path": str(path), "sha256": digest.hexdigest()})
    return hashlib.sha256(_json({"config": config, "inputs": contents}).encode("utf-8")).hexdigest()


def _retryable(error: Exception) -> bool:
    if isinstance(error, OpsCheckError):
        return False
    return isinstance(error, (ConnectionError, TimeoutError)) or getattr(error, "retryable", False) is True


def _review_history(error: Exception) -> dict:
    """Preserve bounded model-loop evidence when the adapter rejects a briefing."""
    history = getattr(error, "review_history", None)
    if not isinstance(history, list):
        return {}
    rounds = []
    for entry in history[:3]:
        if (isinstance(entry, dict) and type(entry.get("round")) is int
                and type(entry.get("approved")) is bool and isinstance(entry.get("feedback"), str)):
            rounds.append({"round": entry["round"], "approved": entry["approved"],
                           "feedback": entry["feedback"][:2000]})
    return {"review_history": rounds}


def _local_briefing(evidence: dict, quality: dict, changes: dict) -> dict:
    quality_counts, change_counts = quality["summary"], changes["summary"]
    summary = (
        f"Quality check: {quality_counts['issues']} issues across "
        f"{quality_counts['affected_rows']} of {quality_counts['rows']} records. "
        f"Snapshot comparison: {change_counts['added']} added, "
        f"{change_counts['removed']} removed, {change_counts['changed']} changed, "
        f"and {change_counts['unchanged']} unchanged records. "
        f"Schema: {len(changes['schema']['added'])} added and "
        f"{len(changes['schema']['removed'])} removed columns. "
        "Totals include findings omitted from the detailed samples."
    )
    actions = []
    for prefix, needed, title, priority in (
        ("V", quality_counts["issues"], "Review the validation findings before importing this export.", "high"),
        ("C", change_counts["total_changes"] or changes["schema"]["added"] or changes["schema"]["removed"],
         "Review the snapshot changes against the expected business updates.", "medium"),
    ):
        ids = [item["id"] for item in evidence["evidence"] if item["id"].startswith(prefix)]
        if needed and ids:
            actions.append({"title": title, "priority": priority, "evidence_ids": ids[:5]})
    return {"mode": "local", "approved": True, "rounds": 0,
            "briefing": {"summary": summary, "actions": actions}, "review_history": []}


def _verify(quality: dict, changes: dict, paths: list[Path], config: dict,
            fingerprint: str) -> dict:
    # Exact comparison to a fresh engine execution checks schemas, source identity,
    # capped samples and uncapped counts together, including unusual edge cases.
    if _fingerprint(paths, config) != fingerprint:
        raise OpsCheckError("Input files changed during this run; start a new run.")
    expected_quality = validate(load_csv(paths[0]), load_rules(paths[1]))
    expected_changes = compare(load_csv(paths[2]), load_csv(paths[3]), config["key"])
    if quality != expected_quality or changes != expected_changes:
        raise OpsCheckError("Verification failed: worker output differs from a fresh execution of the same engines.")
    if _fingerprint(paths, config) != fingerprint:
        raise OpsCheckError("Input files changed during verification; start a new run.")
    return {"verified": True, "checks": ["source_identity", "result_schema", "summary_counts", "engine_recomputation"]}


def _task_outputs(connection: sqlite3.Connection, run_id: str) -> dict:
    return {row["task_id"]: json.loads(row["output_json"])
            for row in connection.execute("SELECT task_id,output_json FROM tasks WHERE run_id=? AND status='succeeded' AND output_json IS NOT NULL", (run_id,))}


def _run_tasks(connection: sqlite3.Connection, run_id: str, paths: list[Path], config: dict,
               fingerprint: str, max_attempts: int, fail_once: str | None) -> None:
    task_rows = connection.execute("SELECT * FROM tasks WHERE run_id=?", (run_id,)).fetchall()
    tasks = {row["task_id"]: dict(row) for row in task_rows if row["task_id"] in PLAN}
    outputs = _task_outputs(connection, run_id)
    allowance_used = {task: 0 for task in PLAN}

    def execute(task: str, total_attempt: int) -> dict:
        if task == fail_once and total_attempt == 1:
            raise TransientTaskError(f"Injected transient failure in {task}; retry is safe.")
        if task == "planner":
            return {"plan": {name: list(dependencies) for name, dependencies in WORKFLOW_PLAN.items()}}
        if task == "quality_agent":
            return validate(load_csv(paths[0]), load_rules(paths[1]))
        if task == "change_agent":
            return compare(load_csv(paths[2]), load_csv(paths[3]), config["key"])
        if task == "verifier":
            return _verify(outputs["quality_agent"], outputs["change_agent"], paths, config, fingerprint)
        from .agents import build_evidence, run_agent_team
        evidence = build_evidence(outputs["quality_agent"], outputs["change_agent"])
        if config["llm_url"]:
            briefing = run_agent_team(evidence, config["llm_url"], config["model"], config["max_rounds"])
        else:
            briefing = _local_briefing(evidence, outputs["quality_agent"], outputs["change_agent"])
        # Keep the ID-to-finding mapping beside the briefing so citations are reviewable.
        return {**briefing, "evidence": evidence}

    # Only the orchestration thread touches SQLite. Workers share immutable results
    # of completed dependencies; no task is submitted until those results exist.
    futures = {}
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="opscheck") as pool:
        while True:
            for task, dependencies in PLAN.items():
                state = tasks[task]
                if state["status"] != "pending" or not all(tasks[dep]["status"] == "succeeded" for dep in dependencies):
                    continue
                state["status"] = "running"
                state["attempts"] += 1
                allowance_used[task] += 1
                with connection:
                    connection.execute("UPDATE tasks SET status='running',attempts=?,error=NULL WHERE run_id=? AND task_id=?",
                                       (state["attempts"], run_id, task))
                    connection.execute("UPDATE runs SET updated_at=? WHERE run_id=?", (_now(), run_id))
                    _event(connection, run_id, "task_started", task, attempt=state["attempts"])
                futures[pool.submit(execute, task, state["attempts"])] = task
            if not futures:
                break
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                task = futures.pop(future)
                state = tasks[task]
                try:
                    output = future.result()
                    serialized = _json(output)  # Serialization is part of task success.
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    will_retry = _retryable(exc) and allowance_used[task] < max_attempts
                    state["status"] = "pending" if will_retry else "failed"
                    with connection:
                        connection.execute("UPDATE tasks SET status=?,error=?,output_json=NULL WHERE run_id=? AND task_id=?",
                                           (state["status"], error, run_id, task))
                        _event(connection, run_id, "task_failed", task, attempt=state["attempts"], error=error,
                               retryable=_retryable(exc), **_review_history(exc))
                        if will_retry:
                            _event(connection, run_id, "task_retrying", task, next_attempt=state["attempts"] + 1)
                else:
                    state["status"] = "succeeded"
                    outputs[task] = output
                    with connection:
                        connection.execute("UPDATE tasks SET status='succeeded',error=NULL,output_json=? WHERE run_id=? AND task_id=?",
                                           (serialized, run_id, task))
                        _event(connection, run_id, "task_succeeded", task, attempt=state["attempts"])
        with connection:
            for task, state in tasks.items():
                if state["status"] == "pending":
                    failed_dependencies = [dep for dep in PLAN[task] if tasks[dep]["status"] != "succeeded"]
                    error = "Blocked by unsuccessful dependencies: " + ", ".join(failed_dependencies)
                    connection.execute("UPDATE tasks SET status='blocked',error=? WHERE run_id=? AND task_id=?",
                                       (error, run_id, task))
                    state["status"] = "blocked"
                    _event(connection, run_id, "task_blocked", task, dependencies=failed_dependencies)
            if not all(state["status"] == "succeeded" for state in tasks.values()):
                reason = "; ".join(row["error"] for row in connection.execute(
                    "SELECT error FROM tasks WHERE run_id=? AND status='failed'", (run_id,)))
                connection.execute("UPDATE runs SET status='FAILED',failure_reason=?,updated_at=? WHERE run_id=?",
                                   (reason, _now(), run_id))
                _event(connection, run_id, "run_completed", status="FAILED", reason=reason)


def _result(connection: sqlite3.Connection, run_id: str, state_dir: Path) -> dict:
    run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    by_id = {row["task_id"]: row for row in connection.execute("SELECT * FROM tasks WHERE run_id=?", (run_id,))}
    ordered = [task for task in PLAN if task in by_id] + [task for task in by_id if task not in PLAN]
    tasks = [{"id": task, "status": by_id[task]["status"], "attempts": by_id[task]["attempts"],
              "error": by_id[task]["error"]} for task in ordered]
    events = [{"id": row["id"], "event": row["event"], "task": row["task"], "timestamp": row["timestamp"],
               **json.loads(row["detail_json"])}
              for row in connection.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,))]
    outputs = _task_outputs(connection, run_id)
    from .approvals import history
    approvals = history(connection, run_id)
    if approvals:
        outputs["briefing_agent"] = approvals[-1]["briefing"]
    pending = next((item for item in approvals if item["status"] == "pending"), None)
    return {"run_id": run_id, "status": run["status"],
            "failure_reason": run["failure_reason"], "approvals": approvals,
            "active_approval_id": pending["id"] if pending else None,
            "approval_iteration": approvals[-1]["iteration"] if approvals else 0,
            "rejected_revisions": sum(item["status"] == "rejected" for item in approvals),
            "report_path": str(state_dir / run_id / "report.html"),
            "mode": "model" if json.loads(run["config_json"])["llm_url"] else "local",
            "plan": {task: list(dependencies) for task, dependencies in WORKFLOW_PLAN.items()}, "tasks": tasks,
            "events": events, "results": {task: outputs[task] for task in ("quality_agent", "change_agent", "briefing_agent") if task in outputs},
            "state_dir": str(state_dir)}


def run_workflow(input_path, rules_path, before_path, after_path, key="order_id",
                 state_dir=Path(".opscheck/runs"), run_id=None, max_attempts=2,
                 fail_once=None, llm_url=None, model=None, max_rounds=2,
                 max_human_revisions=3) -> dict:
    """Execute or resume a workflow; execution failures are returned with evidence.

    max_attempts is the allowance per task for this invocation. Saved attempt totals
    never reset. Execution controls (max_attempts and fail_once) may change on resume;
    content, paths, key and model settings must match the saved fingerprint.
    """
    if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
        raise OpsCheckError("max_attempts must be an integer between 1 and 5.")
    if type(max_rounds) is not int or not 1 <= max_rounds <= 3:
        raise OpsCheckError("max_rounds must be an integer between 1 and 3.")
    if type(max_human_revisions) is not int or not 1 <= max_human_revisions <= 20:
        raise OpsCheckError("max_human_revisions must be an integer between 1 and 20.")
    if not isinstance(key, str) or not key.strip():
        raise OpsCheckError("key must be a nonblank column name.")
    if fail_once not in (None, "quality_agent", "change_agent"):
        raise OpsCheckError("fail_once must be quality_agent or change_agent.")
    if ((llm_url is None) != (model is None)
            or (llm_url is not None and not isinstance(llm_url, str))
            or (model is not None and not isinstance(model, str))):
        raise OpsCheckError("Supply both llm_url and model to use model mode.")
    if llm_url is not None and (not llm_url.strip() or not model.strip()):
        raise OpsCheckError("llm_url and model must be nonblank.")
    if run_id is not None and (not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{32}", run_id) is None):
        raise OpsCheckError("run_id must be an existing 32-character lowercase hexadecimal ID.")
    try:
        paths = [Path(path).expanduser().absolute() for path in (input_path, rules_path, before_path, after_path)]
        state_dir = Path(state_dir).expanduser().absolute()
    except (TypeError, ValueError) as exc:
        raise OpsCheckError("Input and state paths must be filesystem paths.") from exc
    config = {"key": key, "llm_url": llm_url, "model": model, "max_rounds": max_rounds,
              "input_paths": [str(path) for path in paths], "workflow_version": 2,
              "max_human_revisions": max_human_revisions}
    try:
        # Rules are configuration; reject malformed rules before creating run state.
        load_rules(paths[1])
        fingerprint = _fingerprint(paths, config)
        database = _state_path(state_dir, paths)
        resuming = run_id is not None
        run_id = run_id or uuid.uuid4().hex
        with _run_lock(state_dir, run_id, paths):
            connection = _connect(database)
            try:
                with connection:
                    existing = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if resuming:
                        if existing is None:
                            raise OpsCheckError(f"Run {run_id} does not exist in this state directory.")
                        saved_config = json.loads(existing["config_json"])
                        if saved_config.get("workflow_version") == 1:
                            legacy = {k: v for k, v in config.items() if k != "max_human_revisions"}
                            legacy["workflow_version"] = 1
                            fingerprint = _fingerprint(paths, legacy)
                        if existing["fingerprint"] != fingerprint:
                            raise OpsCheckError("Inputs or processing configuration changed; start a new run instead of resuming.")
                        config = saved_config
                        if existing["status"] in ("WAITING_FOR_APPROVAL", "SUCCEEDED") or (
                                existing["status"] == "FAILED" and connection.execute(
                                    "SELECT 1 FROM events WHERE run_id=? AND event='revision_limit_reached'", (run_id,)).fetchone()):
                            return _publish_result(connection, run_id, state_dir, paths)
                        _event(connection, run_id, "run_resumed")
                        for row in connection.execute("SELECT * FROM tasks WHERE run_id=?", (run_id,)).fetchall():
                            if row["status"] == "succeeded":
                                _event(connection, run_id, "task_reused", row["task_id"], attempts=row["attempts"])
                            else:
                                if row["status"] == "running":
                                    _event(connection, run_id, "task_recovered", row["task_id"], previous_status="running")
                                connection.execute("UPDATE tasks SET status='pending',error=NULL,output_json=NULL WHERE run_id=? AND task_id=?",
                                                   (run_id, row["task_id"]))
                        connection.execute("UPDATE runs SET status='RUNNING',failure_reason=NULL,updated_at=? WHERE run_id=?", (_now(), run_id))
                    else:
                        timestamp = _now()
                        connection.execute("INSERT INTO runs(run_id,fingerprint,config_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                           (run_id, fingerprint, _json(config), "PENDING", timestamp, timestamp))
                        for task in PLAN:
                            connection.execute("INSERT INTO tasks(run_id,task_id,status) VALUES(?,?,'pending')", (run_id, task))
                        _event(connection, run_id, "run_created", mode="model" if llm_url else "local")
                        connection.execute("UPDATE runs SET status='RUNNING' WHERE run_id=?", (run_id,))
                _run_tasks(connection, run_id, paths, config, fingerprint, max_attempts, fail_once)
                from .approvals import advance
                advance(connection, run_id, state_dir, config)
                return _publish_result(connection, run_id, state_dir, paths)
            finally:
                connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise OpsCheckError(f"Cannot access workflow input or state: {exc}") from exc


def _publish_result(connection: sqlite3.Connection, run_id: str, state_dir: Path,
                    paths: list[Path]) -> dict:
    """Rebuild disposable reports from committed SQLite state under the run lock."""
    from .artifacts import write_workflow_reports
    result = _result(connection, run_id, state_dir)
    write_workflow_reports(result, paths)
    return result


def list_runs(state_dir=Path(".opscheck/runs")) -> list[dict]:
    """Read run history without creating state for an empty directory."""
    state_dir = Path(state_dir).expanduser().absolute()
    try:
        database = _state_path(state_dir, [], create=False)
        if not database.exists():
            return []
        # mode=rw does not create missing files and allows SQLite to recover its
        # own interrupted journal after a crash. Application queries are read-only.
        connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            return [{"run_id": row["run_id"], "status": row["status"].upper(),
                     "mode": "model" if json.loads(row["config_json"])["llm_url"] else "local",
                     "created_at": row["created_at"], "updated_at": row["updated_at"]}
                    for row in connection.execute("SELECT * FROM runs ORDER BY created_at DESC,run_id")]
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise OpsCheckError(f"Cannot read workflow state: {exc}") from exc
