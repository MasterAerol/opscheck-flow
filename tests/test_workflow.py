"""Exercise recovery, concurrency and verification against real CSV engines."""

from __future__ import annotations

import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from opscheck.core import OpsCheckError
from opscheck import workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "input.csv"
        self.rules = self.root / "rules.json"
        self.before = self.root / "before.csv"
        self.after = self.root / "after.csv"
        self.state = self.root / "state"
        self.input.write_text("order_id,email,total\n1,invalid,10\n2,ok@example.com,20\n", encoding="utf-8")
        self.rules.write_text(json.dumps({"version": 1, "columns": {
            "order_id": {"required": True, "unique": True}, "email": {"type": "email"},
            "total": {"type": "number", "min": 0}}}), encoding="utf-8")
        self.before.write_text("order_id,total\n1,10\n2,20\n", encoding="utf-8")
        self.after.write_text("order_id,total\n1,15\n3,30\n", encoding="utf-8")

    def run_flow(self, **kwargs):
        return workflow.run_workflow(self.input, self.rules, self.before, self.after,
                                     state_dir=self.state, **kwargs)

    @staticmethod
    def tasks(result):
        return {task["id"]: task for task in result["tasks"]}

    def test_detected_business_problems_are_a_successful_workflow(self):
        result = self.run_flow()
        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(result["mode"], "local")
        self.assertTrue(all(self.tasks(result)[task]["status"] == "succeeded" for task in workflow.PLAN))
        self.assertEqual(self.tasks(result)["approval_gate"]["status"], "waiting")
        self.assertEqual(result["results"]["quality_agent"]["status"], "fail")
        self.assertEqual(result["results"]["quality_agent"]["summary"]["issues"], 1)
        self.assertEqual(result["results"]["change_agent"]["summary"]["total_changes"], 3)
        self.assertIn("1 issues", result["results"]["briefing_agent"]["briefing"]["summary"])
        self.assertEqual(result["plan"]["verifier"], ["quality_agent", "change_agent"])
        json.dumps(result, allow_nan=False)

    def test_transient_failure_retries_and_persists_attempt_evidence(self):
        result = self.run_flow(fail_once="quality_agent", max_attempts=2)
        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(self.tasks(result)["quality_agent"]["attempts"], 2)
        events = [event["event"] for event in result["events"] if event["task"] == "quality_agent"]
        self.assertEqual(events, ["task_started", "task_failed", "task_retrying", "task_started", "task_succeeded"])
        # SQLite's own context manager ends a transaction but does not close
        # the connection. Windows requires closure before temporary cleanup.
        with closing(sqlite3.connect(self.state / "runs.sqlite3")) as connection:
            saved = connection.execute("SELECT status,attempts FROM tasks WHERE run_id=? AND task_id='quality_agent'",
                                       (result["run_id"],)).fetchone()
        self.assertEqual(saved, ("succeeded", 2))
        # Catch a leaked connection on every OS, including systems that allow
        # an open database file to be unlinked during temporary cleanup.
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_exhausted_failure_preserves_independent_success(self):
        result = self.run_flow(fail_once="quality_agent", max_attempts=1)
        tasks = self.tasks(result)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(tasks["quality_agent"]["status"], "failed")
        self.assertEqual(tasks["quality_agent"]["attempts"], 1)
        self.assertEqual(tasks["change_agent"]["status"], "succeeded")
        self.assertEqual(tasks["verifier"]["status"], "blocked")
        self.assertEqual(tasks["briefing_agent"]["attempts"], 0)
        self.assertIn("change_agent", result["results"])
        self.assertNotIn("briefing_agent", result["results"])

    def test_failed_database_initialization_closes_its_connection(self):
        database = self.root / "corrupt.sqlite3"
        database.write_bytes(b"This file is not a SQLite database.")
        connect = sqlite3.connect
        connections = []

        def record_connection(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            self.addCleanup(connection.close)
            return connection

        with patch.object(workflow.sqlite3, "connect", side_effect=record_connection):
            with self.assertRaises(sqlite3.DatabaseError):
                workflow._connect(database)
        self.assertEqual(len(connections), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_repeated_transport_failures_stop_at_allowance(self):
        with patch.object(workflow, "validate", side_effect=ConnectionError("temporary upstream failure")) as failing:
            result = self.run_flow(max_attempts=3)
        self.assertEqual(failing.call_count, 3)
        self.assertEqual(self.tasks(result)["quality_agent"]["attempts"], 3)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.tasks(result)["change_agent"]["status"], "succeeded")

    def test_resume_skips_saved_worker_and_uses_new_attempt_allowance(self):
        first = self.run_flow(fail_once="quality_agent", max_attempts=1)
        original_compare = workflow.compare
        with patch.object(workflow, "compare", wraps=original_compare) as compare_calls:
            resumed = self.run_flow(run_id=first["run_id"], max_attempts=2)
        # The one call is the independent verifier: the saved change worker is reused.
        self.assertEqual(compare_calls.call_count, 1)
        self.assertEqual(resumed["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(self.tasks(resumed)["quality_agent"]["attempts"], 2)
        self.assertEqual(self.tasks(resumed)["change_agent"]["attempts"], 1)
        reused = [event["task"] for event in resumed["events"] if event["event"] == "task_reused"]
        self.assertCountEqual(reused, ["planner", "change_agent"])
        self.assertEqual(resumed["results"]["change_agent"], first["results"]["change_agent"])

    def test_finished_run_resume_does_not_execute_tasks_again(self):
        from opscheck.approvals import decide
        waiting = self.run_flow()
        first = decide(waiting["run_id"], "approved", state_dir=self.state)
        with patch.object(workflow, "validate", side_effect=AssertionError("completed task must not rerun")):
            resumed = self.run_flow(run_id=first["run_id"])
        self.assertEqual(resumed["status"], "SUCCEEDED")
        self.assertTrue(all(task["attempts"] == 1 for task in resumed["tasks"]))
        self.assertEqual(resumed["results"], first["results"])

    def test_changed_file_content_refuses_resume(self):
        first = self.run_flow()
        self.input.write_text(self.input.read_text(encoding="utf-8").replace("invalid", "new@example.com"), encoding="utf-8")
        with self.assertRaisesRegex(OpsCheckError, "changed"):
            self.run_flow(run_id=first["run_id"])
        self.assertEqual(workflow.list_runs(self.state)[0]["status"], "WAITING_FOR_APPROVAL")

    def test_changed_processing_configuration_refuses_resume(self):
        first = self.run_flow()
        with self.assertRaisesRegex(OpsCheckError, "changed"):
            self.run_flow(run_id=first["run_id"], key="total")
        with self.assertRaisesRegex(OpsCheckError, "changed"):
            self.run_flow(run_id=first["run_id"], max_rounds=3)

    def test_workers_reach_barrier_in_parallel(self):
        barrier = threading.Barrier(2, timeout=5)
        seen = set()
        counts = {"quality": 0, "changes": 0}
        guard = threading.Lock()
        original_validate, original_compare = workflow.validate, workflow.compare

        def wrap(name, function):
            def checked(*args, **kwargs):
                with guard:
                    counts[name] += 1
                    first_call = counts[name] == 1
                    if first_call:
                        seen.add(threading.current_thread().name)
                if first_call:
                    barrier.wait()
                return function(*args, **kwargs)
            return checked

        with patch.object(workflow, "validate", side_effect=wrap("quality", original_validate)), \
             patch.object(workflow, "compare", side_effect=wrap("changes", original_compare)):
            result = self.run_flow()
        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(len(seen), 2)
        self.assertEqual(counts, {"quality": 2, "changes": 2})

    def test_invalid_input_is_not_retried(self):
        self.before.write_text("order_id,total\n1,10\n1,20\n", encoding="utf-8")
        result = self.run_flow(max_attempts=5)
        tasks = self.tasks(result)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(tasks["change_agent"]["attempts"], 1)
        self.assertEqual(tasks["quality_agent"]["status"], "succeeded")
        self.assertFalse(any(event["event"] == "task_retrying" for event in result["events"]))

    def test_verifier_recomputes_and_rejects_incorrect_worker_counts(self):
        original = workflow.validate
        calls = 0

        def incorrect_first_result(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = copy.deepcopy(original(*args, **kwargs))
            if calls == 1:
                result["summary"]["issues"] += 100
            return result

        with patch.object(workflow, "validate", side_effect=incorrect_first_result):
            result = self.run_flow(max_attempts=5)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.tasks(result)["verifier"]["attempts"], 1)
        self.assertIn("Verification failed", self.tasks(result)["verifier"]["error"])
        self.assertEqual(self.tasks(result)["briefing_agent"]["status"], "blocked")

    def test_input_mutation_during_worker_execution_fails_verification(self):
        original = workflow.validate

        def mutate_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            self.input.write_text("order_id,email,total\n1,other@example.com,11\n", encoding="utf-8")
            return result

        with patch.object(workflow, "validate", side_effect=mutate_after_read):
            result = self.run_flow()
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("changed during", self.tasks(result)["verifier"]["error"])

    def test_failed_model_review_history_is_saved_without_automatic_retry(self):
        from opscheck.agents import AgentError
        error = AgentError("review budget exhausted")
        error.review_history = [
            {"round": 1, "approved": False, "feedback": "The conclusion needs evidence."},
            {"round": 2, "approved": False, "feedback": "The revision remains unsupported."},
        ]
        with patch("opscheck.agents.run_agent_team", side_effect=error) as model_team:
            result = self.run_flow(llm_url="http://127.0.0.1:11434/v1/chat/completions", model="test", max_attempts=5)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["mode"], "model")
        self.assertEqual(model_team.call_count, 1)
        self.assertEqual(self.tasks(result)["briefing_agent"]["attempts"], 1)
        failed_event = next(event for event in result["events"]
                            if event["event"] == "task_failed" and event["task"] == "briefing_agent")
        self.assertEqual(failed_event["review_history"], error.review_history)
        self.assertFalse(failed_event["retryable"])
        self.assertEqual(self.tasks(result)["verifier"]["status"], "succeeded")

    def test_model_transport_failure_retries_without_rerunning_specialists(self):
        from opscheck.agents import TransportError
        briefing = {"mode": "model", "approved": True, "rounds": 1,
                    "briefing": {"summary": "Review one validation issue.", "actions": []},
                    "review_history": [{"round": 1, "approved": True, "feedback": "Supported."}]}
        with patch("opscheck.agents.run_agent_team", side_effect=[TransportError("connection lost"), briefing]) as model_team:
            result = self.run_flow(llm_url="http://127.0.0.1:11434/v1/chat/completions", model="test", max_attempts=2)
        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(model_team.call_count, 2)
        self.assertEqual(self.tasks(result)["briefing_agent"]["attempts"], 2)
        self.assertEqual(self.tasks(result)["quality_agent"]["attempts"], 1)
        self.assertEqual(self.tasks(result)["change_agent"]["attempts"], 1)
        saved_briefing = result["results"]["briefing_agent"]
        self.assertEqual({key: saved_briefing[key] for key in briefing}, briefing)
        evidence, endpoint, model, rounds = model_team.call_args.args
        self.assertEqual(saved_briefing["evidence"], evidence)
        self.assertEqual(evidence["quality_summary"]["issues"], 1)
        self.assertEqual((endpoint, model, rounds), ("http://127.0.0.1:11434/v1/chat/completions", "test", 2))

    def test_same_run_cannot_execute_concurrently(self):
        entered, release = threading.Event(), threading.Event()
        original = workflow.validate
        results, failures = [], []

        def held_validate(*args, **kwargs):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test worker was not released")
            return original(*args, **kwargs)

        def start():
            try:
                results.append(self.run_flow())
            except Exception as exc:
                failures.append(exc)

        with patch.object(workflow, "validate", side_effect=held_validate):
            thread = threading.Thread(target=start)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                run_id = workflow.list_runs(self.state)[0]["run_id"]
                with self.assertRaisesRegex(OpsCheckError, "already executing"):
                    self.run_flow(run_id=run_id)
            finally:
                release.set()
                thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertFalse(failures)
        self.assertEqual(results[0]["status"], "WAITING_FOR_APPROVAL")

    @unittest.skipUnless(os.name in ("posix", "nt"), "requires supported OS advisory locks")
    def test_process_crash_releases_lock_and_resume_recovers_running_task(self):
        script = """
import os
import sys
from opscheck import workflow

def crash(*args, **kwargs):
    os._exit(23)

workflow.validate = crash
workflow.run_workflow(*sys.argv[1:5], state_dir=sys.argv[5])
"""
        project = str(Path(__file__).resolve().parents[1])
        environment = os.environ.copy()
        environment["PYTHONPATH"] = project + os.pathsep + environment.get("PYTHONPATH", "")
        process = subprocess.run([sys.executable, "-c", script, str(self.input), str(self.rules),
                                  str(self.before), str(self.after), str(self.state)],
                                 env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(process.returncode, 23, process.stderr)
        interrupted = workflow.list_runs(self.state)[0]
        self.assertEqual(interrupted["status"], "RUNNING")
        resumed = self.run_flow(run_id=interrupted["run_id"])
        self.assertEqual(resumed["status"], "WAITING_FOR_APPROVAL")
        self.assertGreaterEqual(self.tasks(resumed)["quality_agent"]["attempts"], 2)
        self.assertTrue(any(event["event"] == "task_recovered" and event["task"] == "quality_agent"
                            for event in resumed["events"]))

    def test_state_database_cannot_overwrite_an_input(self):
        self.state.mkdir()
        self.input = self.state / "runs.sqlite3"
        original = b"order_id,email,total\n1,test@example.com,10\n"
        self.input.write_bytes(original)
        with self.assertRaisesRegex(OpsCheckError, "overwrite an input"):
            self.run_flow()
        self.assertEqual(self.input.read_bytes(), original)

    def test_state_database_hardlink_cannot_modify_an_input(self):
        self.state.mkdir()
        original = self.input.read_bytes()
        os.link(self.input, self.state / "runs.sqlite3")
        with self.assertRaisesRegex(OpsCheckError, "hardlinks"):
            self.run_flow()
        self.assertEqual(self.input.read_bytes(), original)

    @unittest.skipUnless(os.name == "posix", "symlink creation on Windows may require privileges")
    def test_state_database_and_sqlite_sidecar_symlinks_are_rejected(self):
        self.state.mkdir()
        original = self.input.read_bytes()
        for name in ("runs.sqlite3", "runs.sqlite3-journal", "runs.sqlite3-wal", "runs.sqlite3-shm"):
            with self.subTest(name=name):
                path = self.state / name
                path.symlink_to(self.input)
                with self.assertRaisesRegex(OpsCheckError, "symlink"):
                    self.run_flow()
                self.assertEqual(self.input.read_bytes(), original)
                path.unlink()

    def test_invalid_arguments_do_not_create_state(self):
        for kwargs in ({"max_attempts": 0}, {"max_attempts": 6}, {"max_attempts": True},
                       {"max_rounds": 0}, {"max_rounds": 4}, {"fail_once": "planner"},
                       {"key": ""}, {"llm_url": "http://localhost"}, {"model": "small"},
                       {"run_id": "../../elsewhere"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(OpsCheckError):
                self.run_flow(**kwargs)
        self.assertFalse(self.state.exists())

    def test_invalid_rules_are_rejected_before_state_creation(self):
        self.rules.write_text('{"version":1,"columns":{"email":{"regex":".*"}}}', encoding="utf-8")
        with self.assertRaises(OpsCheckError):
            self.run_flow()
        self.assertFalse(self.state.exists())

    def test_missing_resume_id_is_an_error(self):
        with self.assertRaisesRegex(OpsCheckError, "does not exist"):
            self.run_flow(run_id="a" * 32)

    def test_list_runs_is_read_only_for_empty_location_and_lists_outcomes(self):
        self.assertEqual(workflow.list_runs(self.state), [])
        self.assertFalse(self.state.exists())
        succeeded = self.run_flow()
        failed = self.run_flow(fail_once="change_agent", max_attempts=1)
        history = workflow.list_runs(self.state)
        self.assertEqual([row["run_id"] for row in history], [failed["run_id"], succeeded["run_id"]])
        self.assertEqual([row["status"] for row in history], ["FAILED", "WAITING_FOR_APPROVAL"])
        self.assertTrue(all(row["mode"] == "local" for row in history))


if __name__ == "__main__":
    unittest.main()
