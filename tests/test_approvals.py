"""Durable human approval acceptance, crash recovery, and real-process races."""
from contextlib import closing
from copy import deepcopy
from html import escape
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from opscheck import approvals, workflow
from opscheck.core import OpsCheckError
from opscheck.revision import revise_briefing

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "opscheck" / "examples"


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.paths = [EXAMPLES / name for name in
                      ("orders-messy.csv", "rules.json", "orders-before.csv", "orders-after.csv")]

    def start(self, **kwargs):
        return workflow.run_workflow(*self.paths, state_dir=self.state, **kwargs)

    def decide(self, run, decision="approved", **kwargs):
        return approvals.decide(run["run_id"], decision, state_dir=self.state, **kwargs)

    def inspect(self, run):
        return approvals.inspect_run(run["run_id"], self.state)

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "opscheck", *map(str, args), "--state-dir", str(self.state)],
                              cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                              capture_output=True, encoding="utf-8", timeout=30)

    def worker_attempts(self, run):
        return {task["id"]: task["attempts"] for task in run["tasks"] if task["id"] in workflow.PLAN}

    def test_checkpoint_is_persisted_without_success_event(self):
        run = self.start()
        self.assertEqual(run["status"], "WAITING_FOR_APPROVAL")
        saved = self.inspect(run)
        self.assertEqual(saved, run)
        self.assertEqual(len(saved["approvals"]), 1)
        item = saved["approvals"][0]
        self.assertEqual((item["iteration"], item["status"]), (1, "pending"))
        self.assertIsNone(item["decided_at"])
        self.assertEqual(json.loads(Path(item["briefing_reference"]).read_text(encoding="utf-8")), item["briefing"])
        events = [e["event"] for e in run["events"]]
        self.assertIn("approval_requested", events)
        self.assertIn("workflow_paused", events)
        self.assertNotIn("run_completed", events)

    def test_approval_persists_identity_time_and_skips_all_workers(self):
        run = self.start()
        with patch.object(workflow, "_run_tasks", side_effect=AssertionError("must not rerun")):
            final = self.decide(run, reviewer="Aerol", comment="Ready.")
        self.assertEqual(final["status"], "SUCCEEDED")
        item = final["approvals"][0]
        self.assertEqual((item["status"], item["decision"], item["reviewer"], item["comment"]),
                         ("approved", "approved", "Aerol", "Ready."))
        self.assertGreaterEqual(item["decided_at"], item["created_at"])
        self.assertEqual(self.worker_attempts(final), self.worker_attempts(run))
        self.assertEqual([e["event"] for e in final["events"]][-3:],
                         ["approval_approved", "workflow_resumed", "run_completed"])

    def test_reject_revise_reject_revise_approve_keeps_exact_history(self):
        first = self.start()
        second = self.decide(first, "rejected", reviewer="Aerol", comment="Explain validation records.")
        old = deepcopy(second["approvals"][0])
        self.assertEqual(second["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual((old["reviewer"], old["comment"]), ("Aerol", "Explain validation records."))
        self.assertEqual(old["briefing"], first["results"]["briefing_agent"])
        self.assertEqual(second["results"]["briefing_agent"]["revision"]["number"], 1)
        third = self.decide(second, "rejected", reviewer="Reviewer 2", comment="Include changed order IDs.")
        old_second = deepcopy(third["approvals"][1])
        final = self.decide(third, reviewer="Aerol")
        self.assertEqual(final["status"], "SUCCEEDED")
        self.assertEqual(final["approvals"][:2], [old, old_second])
        self.assertEqual([a["iteration"] for a in final["approvals"]], [1, 2, 3])
        self.assertEqual([a["status"] for a in final["approvals"]], ["rejected", "rejected", "approved"])
        self.assertEqual(final["rejected_revisions"], 2)
        self.assertEqual(self.worker_attempts(first), self.worker_attempts(final))
        self.assertEqual(sum(e["event"] == "revision_completed" for e in final["events"]), 2)

    def test_revision_is_deterministic_and_keeps_verified_facts(self):
        run = self.start()
        outputs = run["results"]
        current = deepcopy(outputs["briefing_agent"])
        verifier = {"verified": True, "checks": ["engine_recomputation"]}
        args = (current, verifier, outputs["quality_agent"], outputs["change_agent"], "Explain changes", 1)
        revised = revise_briefing(*args)
        self.assertEqual(revised, revise_briefing(*args))
        self.assertEqual(current, outputs["briefing_agent"])
        section = revised["revision"]["sections"][0]
        self.assertEqual(section["findings"], outputs["change_agent"]["findings"])
        self.assertIn("changes", section["title"])
        self.assertEqual(revised["revision"]["sections"][1]["findings"], outputs["quality_agent"]["findings"])
        with self.assertRaises(OpsCheckError):
            revise_briefing(current, {"verified": False}, {}, {}, "feedback", 1)

    def test_revision_limit_is_terminal_and_not_bypassed_by_resume(self):
        run = self.start(max_human_revisions=3)
        for _ in range(3):
            run = self.decide(run, "rejected", comment="Clarify validation.")
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(run["failure_reason"], "Human approval revision limit reached after 3 rejected versions.")
        self.assertEqual(len(run["approvals"]), 3)
        self.assertIsNone(run["active_approval_id"])
        self.assertEqual(sum(e["event"] == "revision_limit_reached" for e in run["events"]), 1)
        self.assertEqual(approvals.resume_run(run["run_id"], self.state), run)
        self.assertEqual(self.start(run_id=run["run_id"]), run)
        with self.assertRaisesRegex(OpsCheckError, "failed run"):
            self.decide(run)

    def test_limit_one_fails_without_revision(self):
        run = self.decide(self.start(max_human_revisions=1), "rejected", comment="Not ready")
        self.assertEqual(run["status"], "FAILED")
        self.assertEqual(len(run["approvals"]), 1)
        self.assertFalse(any(e["event"] == "revision_started" for e in run["events"]))

    def test_resume_waiting_is_a_noop(self):
        run = self.start()
        with patch.object(workflow, "_run_tasks", side_effect=AssertionError("must not rerun")):
            self.assertEqual(self.start(run_id=run["run_id"]), run)
            self.assertEqual(approvals.resume_run(run["run_id"], self.state), run)
        result = self.command("resume", run["run_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("waiting for human approval", result.stdout)
        self.assertEqual(self.inspect(run), run)

    def test_invalid_decisions_do_not_mutate_state(self):
        run = self.start()
        for kwargs in ({"decision": "rejected"}, {"decision": "rejected", "comment": " \t"},
                       {"decision": "other"}, {"reviewer": " "}, {"comment": 42}):
            with self.subTest(kwargs=kwargs), self.assertRaises(OpsCheckError):
                self.decide(run, **kwargs)
        for args in (("reject", run["run_id"]), ("reject", run["run_id"], "--comment", "   ")):
            result = self.command(*args)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.inspect(run), run)

    def test_missing_completed_and_failed_runs(self):
        with self.assertRaisesRegex(OpsCheckError, "Run not found"):
            approvals.decide("a" * 32, "approved", state_dir=self.state)
        self.assertFalse(self.state.exists())
        final = self.decide(self.start())
        for decision in ("approved", "rejected"):
            with self.assertRaisesRegex(OpsCheckError, "already complete"):
                self.decide(final, decision, comment="feedback")
        failed = self.start(fail_once="quality_agent", max_attempts=1)
        with self.assertRaisesRegex(OpsCheckError, "Cannot approve a failed run"):
            self.decide(failed)
        with self.assertRaisesRegex(OpsCheckError, "Run not found"):
            approvals.inspect_run("b" * 32, self.state)

    def test_old_version_cannot_resolve_new_pending_request(self):
        first = self.start()
        second = self.decide(first, "rejected", comment="Explain affected rows")
        for decision in ("approved", "rejected"):
            with self.assertRaisesRegex(OpsCheckError, "already be resolved"):
                self.decide(second, decision, approval_id=first["active_approval_id"], comment="Old decision")
        self.assertEqual(self.inspect(second), second)

    def test_database_rejects_duplicate_pending_and_history_mutations(self):
        run = self.decide(self.start(), "rejected", comment="Explain rows")
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection:
            for sql in ("UPDATE approvals SET comment='changed' WHERE iteration=1",
                        "DELETE FROM approvals WHERE iteration=1",
                        "UPDATE approvals SET briefing_json='{}' WHERE iteration=2",
                        "INSERT INTO approvals SELECT 'different',run_id,task_name,3,status,decision,reviewer,comment,created_at,decided_at,briefing_reference,briefing_json FROM approvals WHERE iteration=2"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError), connection:
                    connection.execute(sql)
        self.assertEqual(self.inspect(run), run)

    def test_html_comments_reviewer_and_revision_feedback_are_escaped(self):
        attack = '<script>alert("x")</script>'
        run = self.decide(self.start(), "rejected", comment=attack, reviewer=attack)
        html = Path(run["report_path"]).read_text(encoding="utf-8")
        self.assertNotIn("<script", html)
        self.assertIn(escape(attack, quote=True), html)
        self.assertIn("Approval History", html)
        self.assertIn("Version 1", html)
        self.assertIn("Version 2", html)
        final = self.decide(run)
        self.assertIn("Approval Status: Approved", Path(final["report_path"]).read_text(encoding="utf-8"))

    def test_fresh_cli_process_rejects_revises_and_approves(self):
        result = self.command("flow-demo", "--fail-once", "quality_agent")
        self.assertEqual(result.returncode, 0, result.stderr)
        run_id = workflow.list_runs(self.state)[0]["run_id"]
        initial = approvals.inspect_run(run_id, self.state)
        self.assertIn("WAITING_FOR_APPROVAL", self.command("runs").stdout)
        rejected = self.command("reject", run_id, "--reviewer", "Aerol", "--comment", "Explain validation rows.")
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        self.assertIn("revision_agent completed", rejected.stdout)
        history = self.command("approvals", run_id)
        self.assertIn("#1 REJECTED", history.stdout)
        self.assertIn("#2 PENDING", history.stdout)
        final_command = self.command("approve", run_id, "--reviewer", "Aerol")
        self.assertEqual(final_command.returncode, 0, final_command.stderr)
        self.assertIn("SUCCEEDED", final_command.stdout)
        final = approvals.inspect_run(run_id, self.state)
        self.assertEqual(self.worker_attempts(initial), self.worker_attempts(final))
        self.assertEqual(self.command("run", run_id).returncode, 0)

    def test_fresh_process_direct_approval(self):
        run = self.start()
        result = self.command("approve", run["run_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.inspect(run)["status"], "SUCCEEDED")

    def test_two_processes_only_one_can_decide_same_version(self):
        script = '''
import sys
from opscheck.approvals import decide
from opscheck.core import OpsCheckError
print("ready", flush=True)
sys.stdin.readline()
try:
    decide(sys.argv[1], sys.argv[4], state_dir=sys.argv[2], approval_id=sys.argv[3] or None, comment="Explain rows")
except OpsCheckError as exc:
    print(str(exc))
    sys.exit(2)
'''
        for choices in (("approved", "approved"), ("rejected", "rejected"), ("approved", "rejected")):
            with self.subTest(choices=choices):
                run = self.start()
                expected = "" if choices == ("approved", "approved") else run["active_approval_id"]
                children = [subprocess.Popen([sys.executable, "-c", script, run["run_id"], str(self.state), expected, choice],
                            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                            for choice in choices]
                try:
                    for child in children:
                        self.assertEqual(child.stdout.readline().strip(), "ready")
                    for child in children:
                        child.stdin.write("go\n")
                        child.stdin.flush()
                    outputs = [child.communicate(timeout=30) for child in children]
                    self.assertEqual(sorted(child.returncode for child in children), [0, 2], outputs)
                    saved = self.inspect(run)
                    self.assertEqual(sum(e["event"] in ("approval_approved", "approval_rejected") for e in saved["events"]), 1)
                    self.assertLessEqual(len(saved["approvals"]), 2)
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                        child.communicate()

    def crash_decision(self, run, boundary, decision="rejected"):
        script = '''
import os, sys
from opscheck import approvals
boundary = sys.argv[3]
if boundary == "decision":
    approvals.advance = lambda *args: os._exit(23)
elif boundary == "revision":
    approvals.revise_briefing = lambda *args: os._exit(23)
elif boundary == "request":
    approvals._request = lambda *args: os._exit(23)
approvals.decide(sys.argv[1], sys.argv[4], comment="Explain affected rows", state_dir=sys.argv[2])
'''
        process = subprocess.run([sys.executable, "-c", script, run["run_id"], str(self.state), boundary, decision],
                                 cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(process.returncode, 23, process.stderr)

    def test_recovery_after_decision_commit_and_during_revision_and_before_gate(self):
        for boundary in ("decision", "revision", "request"):
            with self.subTest(boundary=boundary):
                run = self.start()
                self.crash_decision(run, boundary)
                saved = self.inspect(run)
                self.assertEqual(saved["status"], "RUNNING")
                self.assertEqual(saved["approvals"][0]["status"], "rejected")
                command = self.command("resume", run["run_id"])
                self.assertEqual(command.returncode, 0, command.stderr)
                resumed = self.inspect(run)
                self.assertEqual(resumed["status"], "WAITING_FOR_APPROVAL")
                self.assertEqual(len(resumed["approvals"]), 2)
                self.assertEqual(resumed["approvals"][0], saved["approvals"][0])
                self.assertEqual(self.worker_attempts(resumed), self.worker_attempts(run))
                self.assertEqual(sum(e["event"] == "revision_completed" for e in resumed["events"]), 1)
                self.assertEqual(self.decide(resumed)["status"], "SUCCEEDED")

    def test_recovery_after_approved_decision_commit(self):
        run = self.start()
        self.crash_decision(run, "decision", "approved")
        self.assertEqual(self.command("resume", run["run_id"]).returncode, 0)
        final = self.inspect(run)
        self.assertEqual(final["status"], "SUCCEEDED")
        self.assertEqual(self.worker_attempts(run), self.worker_attempts(final))

    def test_revision_failure_is_saved_and_retry_reuses_original_workers(self):
        run = self.start()
        with patch.object(approvals, "revise_briefing", side_effect=ValueError("bad revision")):
            failed = self.decide(run, "rejected", comment="Explain rows")
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("bad revision", failed["failure_reason"])
        resumed = approvals.resume_run(run["run_id"], self.state)
        self.assertEqual(resumed["status"], "WAITING_FOR_APPROVAL")
        self.assertIsNone(resumed["failure_reason"])
        self.assertEqual(self.worker_attempts(run), self.worker_attempts(resumed))

    def test_report_write_failure_does_not_lose_decision(self):
        run = self.start()
        with patch("opscheck.artifacts._atomic_write", side_effect=PermissionError("report locked")):
            with self.assertRaises(OpsCheckError):
                self.decide(run)
        self.assertEqual(self.inspect(run)["status"], "SUCCEEDED")
        resumed = approvals.resume_run(run["run_id"], self.state)
        self.assertEqual(json.loads(Path(resumed["report_path"]).with_suffix('.json').read_text(encoding="utf-8"))["status"], "SUCCEEDED")

    def test_inspection_reads_sqlite_without_reports(self):
        run = self.start()
        Path(run["report_path"]).unlink()
        self.assertEqual(self.inspect(run), run)

    def test_connections_close_on_success_and_errors_and_database_can_be_removed(self):
        opened = []
        original = sqlite3.connect
        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            opened.append(connection)
            self.addCleanup(connection.close)
            return connection
        with patch.object(workflow.sqlite3, "connect", side_effect=connect):
            run = self.start()
            self.inspect(run)
            self.decide(run, "rejected", comment="Clarify")
            self.decide(run)
            with self.assertRaises(OpsCheckError):
                self.decide(run)
            workflow.list_runs(self.state)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        (self.state / "runs.sqlite3").unlink()

    def test_limit_configuration_is_validated_and_cannot_change_on_resume(self):
        for limit in (0, 21, True, "3"):
            with self.subTest(limit=limit), self.assertRaises(OpsCheckError):
                self.start(max_human_revisions=limit)
        self.assertFalse(self.state.exists())
        run = self.start(max_human_revisions=2)
        with self.assertRaisesRegex(OpsCheckError, "configuration changed"):
            self.start(run_id=run["run_id"], max_human_revisions=3)
        self.assertEqual(approvals.resume_run(run["run_id"], self.state), run)

    def test_checkpoint_transaction_rolls_back_and_recovers_once(self):
        original_event = approvals._event
        def fail_event(connection, run_id, event, *args, **kwargs):
            if event == "workflow_paused":
                raise RuntimeError("crash before checkpoint commit")
            return original_event(connection, run_id, event, *args, **kwargs)
        with patch.object(approvals, "_event", side_effect=fail_event):
            with self.assertRaisesRegex(RuntimeError, "before checkpoint"):
                self.start()
        run_id = workflow.list_runs(self.state)[0]["run_id"]
        saved = approvals.inspect_run(run_id, self.state)
        self.assertEqual(saved["status"], "RUNNING")
        self.assertEqual(saved["approvals"], [])
        self.assertFalse(any(e["event"] == "approval_requested" for e in saved["events"]))
        recovered = approvals.resume_run(run_id, self.state)
        self.assertEqual(recovered["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(len(recovered["approvals"]), 1)
        self.assertEqual(self.worker_attempts(recovered), self.worker_attempts(saved))

    def test_decision_transaction_rolls_back_on_event_error(self):
        run = self.start()
        with patch.object(approvals, "_event", side_effect=RuntimeError("event write failed")):
            with self.assertRaisesRegex(RuntimeError, "event write failed"):
                self.decide(run)
        self.assertEqual(self.inspect(run), run)

    def test_decisions_use_persisted_evidence_without_source_files(self):
        copies = []
        for path in self.paths:
            copy = Path(self.temp.name) / path.name
            copy.write_bytes(path.read_bytes())
            copies.append(copy)
        run = workflow.run_workflow(*copies, state_dir=self.state)
        for path in copies:
            path.unlink()
        revised = self.decide(run, "rejected", comment="Explain changed IDs")
        final = self.decide(revised)
        self.assertEqual(final["status"], "SUCCEEDED")
        self.assertEqual(self.worker_attempts(final), self.worker_attempts(run))

    def test_report_alias_cannot_overwrite_source_during_decision(self):
        run = self.start()
        report = Path(run["report_path"])
        report.unlink()
        source = Path(self.temp.name) / "protected.csv"
        source.write_bytes(b"protected")
        # Use a copy, never create test aliases to tracked example files.
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection, connection:
            config = json.loads(connection.execute("SELECT config_json FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()[0])
            config["input_paths"][0] = str(source)
            connection.execute("UPDATE runs SET config_json=? WHERE run_id=?", (json.dumps(config), run["run_id"]))
        os.link(source, report)
        with self.assertRaisesRegex(OpsCheckError, "overwrite an input"):
            self.decide(run)
        self.assertEqual(source.read_bytes(), b"protected")
        self.assertEqual(self.inspect(run)["status"], "SUCCEEDED")

    def test_existing_milestone_one_schema_migrates_without_losing_evidence(self):
        # Construct the actual original schema from a failed run, without approvals.
        run = self.start(fail_once="quality_agent", max_attempts=1)
        database = self.state / "runs.sqlite3"
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            saved_run = dict(connection.execute("SELECT * FROM runs").fetchone())
            tasks = [tuple(row) for row in connection.execute("SELECT * FROM tasks")]
            events = [tuple(row) for row in connection.execute("SELECT * FROM events")]
        config = json.loads(saved_run["config_json"])
        config.pop("max_human_revisions")
        config["workflow_version"] = 1
        database.unlink()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript('''
                CREATE TABLE runs(run_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,config_json TEXT NOT NULL,
                    status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE tasks(run_id TEXT NOT NULL REFERENCES runs(run_id),task_id TEXT NOT NULL,status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,error TEXT,output_json TEXT,PRIMARY KEY(run_id,task_id));
                CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL REFERENCES runs(run_id),
                    event TEXT NOT NULL,task TEXT,timestamp TEXT NOT NULL,detail_json TEXT NOT NULL);
            ''')
            connection.execute("INSERT INTO runs VALUES(?,?,?,?,?,?)", (run["run_id"], workflow._fingerprint(self.paths, config),
                json.dumps(config), "failed", saved_run["created_at"], saved_run["updated_at"]))
            connection.executemany("INSERT INTO tasks VALUES(?,?,?,?,?,?)", tasks)
            connection.executemany("INSERT INTO events VALUES(?,?,?,?,?,?)", events)
        resumed = approvals.resume_run(run["run_id"], self.state)
        self.assertEqual(resumed["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(self.worker_attempts(resumed)["change_agent"], 1)
        self.assertEqual(resumed["events"][:len(run["events"])], run["events"])
        self.assertEqual(self.decide(resumed)["status"], "SUCCEEDED")


if __name__ == "__main__":
    unittest.main()
