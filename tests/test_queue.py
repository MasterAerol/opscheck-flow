"""Queue identity, leases, process races, recovery, and installed-facing CLI tests."""
from contextlib import closing
from datetime import datetime
from html import escape
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from opscheck import approvals, event_store, ingestion, queue, queue_store as store, worker, workflow
from opscheck.core import OpsCheckError

ROOT = Path(__file__).resolve().parents[1]


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        shutil.copytree(ROOT / "opscheck/examples/inbox", self.inbox)
        self.manifest = self.inbox / "job-001.opscheck.json"
        self.state = self.root / "state"
        self.now = store.clock()

    def enqueue(self, **kwargs):
        return queue.enqueue(self.manifest, self.state, **kwargs)

    def inspect(self, job):
        return queue.inspect_job(job["id"], self.state)

    def claim(self, worker_id="worker-a", **kwargs):
        return queue.claim(self.state, worker_id=worker_id, **kwargs)

    def work(self, **kwargs):
        return worker.process(self.claim(), self.state, **kwargs)

    def clock(self):
        context = patch.object(store, "clock", side_effect=lambda: self.now)
        context.start()
        self.addCleanup(context.stop)

    def ready(self, job):
        self.now = datetime.fromisoformat(job["available_at"]).timestamp() + .001

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "opscheck", *map(str, args), "--state-dir", str(self.state)],
                              cwd=ROOT, capture_output=True, encoding="utf-8", timeout=30,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    def assert_identity(self, first, second):
        for name in ("id", "ingestion_id", "workflow_run_id"):
            self.assertEqual(first[name], second[name])

    def test_enqueue_reserves_without_executing_any_tasks(self):
        with patch.object(ingestion, "_execute", side_effect=AssertionError("enqueue executed workflow")):
            job = self.enqueue()
        self.assertEqual(job["status"], "QUEUED")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(len(workflow.list_runs(self.state)), 0)
        event = ingestion.inspect_event(job["ingestion_id"], self.state)
        self.assertEqual(event["workflow_run_id"], job["workflow_run_id"])
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM tasks").fetchone()[0], 0)

    def test_duplicate_and_renamed_manifest_reuse_job_and_reservation(self):
        job = self.enqueue(max_attempts=4, priority=10)
        duplicate = self.enqueue(max_attempts=1, priority=-10)
        self.assert_identity(job, duplicate)
        self.assertTrue(duplicate["duplicate"])
        self.assertFalse(duplicate["job_created"])
        self.assertEqual(duplicate["max_attempts"], 4)
        other = self.inbox / "copy.opscheck.json"
        shutil.copyfile(self.manifest, other)
        self.assert_identity(job, queue.enqueue(other, self.state))
        self.assertEqual(len(queue.list_jobs(self.state)), 1)
        self.assertEqual(len(ingestion.list_events(self.state)), 1)

    def test_sync_ingest_then_enqueue_and_queue_then_sync_ingest(self):
        event = ingestion.ingest(self.manifest, self.state)
        job = self.enqueue()
        self.assertEqual(job["workflow_run_id"], event["workflow_run_id"])
        self.assertEqual(job["status"], "WAITING_FOR_APPROVAL")
        self.assertIsNone(self.claim())
        other_state = self.root / "other"
        queued = queue.enqueue(self.manifest, other_state)
        synced = ingestion.ingest(self.manifest, other_state)
        self.assertEqual(queued["workflow_run_id"], synced["workflow_run_id"])
        self.assertEqual(queue.inspect_job(queued["id"], other_state)["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(len(workflow.list_runs(other_state)), 1)

    def test_invalid_manifest_creates_diagnostic_not_job(self):
        self.manifest.write_text('{"module":"os","function":"system"}', encoding="utf-8")
        with self.assertRaises(OpsCheckError):
            self.enqueue()
        self.assertEqual(queue.list_jobs(self.state), [])
        self.assertEqual(ingestion.list_events(self.state)[0]["status"], "INVALID")

    def test_queued_scan_is_idempotent_and_isolates_invalid(self):
        shutil.copyfile(self.manifest, self.inbox / "copy.opscheck.json")
        (self.inbox / "invalid.opscheck.json").write_text("{}", encoding="utf-8")
        first = queue.scan(self.inbox, self.state)
        second = queue.scan(self.inbox, self.state)
        self.assertEqual((first["scanned"], first["new_queue_jobs"], first["existing_jobs"], first["failed"]), (3, 1, 1, 1))
        self.assertEqual((second["new_queue_jobs"], second["existing_jobs"]), (0, 2))
        self.assertEqual(workflow.list_runs(self.state), [])

    def test_claim_records_secure_token_owner_attempt_and_expiry(self):
        self.clock()
        job = self.enqueue()
        claimed = self.claim(lease_seconds=30)
        self.assert_identity(job, claimed)
        self.assertEqual(claimed["status"], "LEASED")
        self.assertRegex(claimed["lease_token"], r"^[0-9a-f]{64}$")
        self.assertEqual(claimed["lease_owner"], "worker-a")
        self.assertEqual(claimed["attempts"], 1)
        self.assertEqual(claimed["lease_expires_at"], store.stamp(self.now + 30))
        self.assertEqual(claimed["attempt_history"][0]["worker_id"], "worker-a")
        self.assertNotIn("lease_token", self.inspect(job))
        self.assertNotIn("lease_token", self.inspect(job)["attempt_history"][0])

    def test_future_job_is_skipped_until_available(self):
        self.clock()
        job = self.enqueue()
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection, connection:
            connection.execute("UPDATE queue_jobs SET available_at=? WHERE id=?", (store.stamp(self.now + 60), job["id"]))
        self.assertIsNone(self.claim())
        self.now += 60
        self.assertIsNotNone(self.claim())

    def test_priority_selects_highest_then_oldest(self):
        first = self.enqueue(priority=-1)
        document = json.loads(self.manifest.read_text())
        document["key"] = "email"
        second_path = self.inbox / "second.opscheck.json"
        second_path.write_text(json.dumps(document), encoding="utf-8")
        second = queue.enqueue(second_path, self.state, priority=10)
        self.assertEqual(self.claim()["id"], second["id"])
        self.assertEqual(self.claim()["id"], first["id"])

    def test_heartbeat_extends_lease_and_blocks_competing_claim(self):
        self.clock()
        self.enqueue()
        claim = self.claim(lease_seconds=5)
        self.now += 4
        queue.heartbeat(claim["id"], claim["lease_token"], self.state, lease_seconds=5)
        job = self.inspect(claim)
        self.assertGreater(job["lease_expires_at"], claim["lease_expires_at"])
        self.assertEqual(job["attempt_history"][0]["heartbeat_at"], store.stamp())
        self.now += 2
        self.assertIsNone(self.claim("worker-b"))
        self.assertEqual(len(job["history"]), 2)  # Heartbeats do not grow audit history.

    def test_expired_lease_reclaims_same_identity_with_new_token(self):
        self.clock()
        self.enqueue()
        first = self.claim(lease_seconds=5)
        self.now += 6
        second = self.claim("worker-b")
        self.assert_identity(first, second)
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["attempt_history"][0]["outcome"], "EXPIRED")
        self.assertTrue(second["attempt_history"][0]["lease_lost"])
        self.assertIn("lease_expired", [e["event"] for e in second["history"]])

    def test_expired_token_cannot_renew_even_before_reclaim(self):
        self.clock()
        self.enqueue()
        claim = self.claim(lease_seconds=5)
        self.now += 5
        with self.assertRaises(queue.LeaseLostError):
            queue.heartbeat(claim["id"], claim["lease_token"], self.state)
        self.assertEqual(self.inspect(claim)["lease_expires_at"], claim["lease_expires_at"])

    def test_expiry_reclaim_works_while_wall_clock_advances_during_transaction(self):
        self.clock()
        self.enqueue()
        first = self.claim(lease_seconds=5)
        self.now += 6
        def advancing_clock():
            self.now += .001
            return self.now
        with patch.object(store, "clock", side_effect=advancing_clock):
            second = self.claim("worker-b")
        self.assertIsNotNone(second)
        self.assert_identity(first, second)

    def test_stale_token_cannot_heartbeat_complete_fail_or_deadletter(self):
        self.clock()
        self.enqueue()
        first = self.claim(lease_seconds=5)
        self.now += 6
        second = self.claim("worker-b")
        for operation in (lambda: queue.heartbeat(first["id"], first["lease_token"], self.state),
                          lambda: queue.finish(first["id"], first["lease_token"], self.state),
                          lambda: queue.finish(first["id"], first["lease_token"], self.state, error="stale failure"),
                          lambda: queue.finish(first["id"], first["lease_token"], self.state, error="terminal", terminal=True)):
            with self.assertRaises(queue.LeaseLostError):
                operation()
        saved = self.inspect(second)
        self.assertEqual(saved["status"], "LEASED")
        self.assertEqual(saved["lease_owner"], "worker-b")
        self.assertEqual(saved["attempts"], 2)
        self.assertEqual(saved["lease_expires_at"], second["lease_expires_at"])

    def test_worker_reaches_approval_despite_business_findings(self):
        job = self.enqueue()
        result = self.work()
        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertIsNone(result["processing_error"])
        self.assertIsNone(result["lease_owner"])
        self.assertIsNone(result["lease_expires_at"])
        self.assertEqual(result["attempt_history"][0]["outcome"], "WAITING_FOR_APPROVAL")
        self.assertEqual(len(workflow.list_runs(self.state)), 1)
        self.assert_identity(job, result)

    def test_human_revision_and_approval_preserve_job_and_specialist_attempts(self):
        job = self.enqueue()
        self.work()
        before = approvals.inspect_run(job["workflow_run_id"], self.state)
        approvals.decide(job["workflow_run_id"], "rejected", "Aerol", "Explain changes.", self.state)
        revised = self.inspect(job)
        self.assertEqual(revised["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(revised["attempts"], 1)
        final = approvals.decide(job["workflow_run_id"], "approved", "Aerol", state_dir=self.state)
        self.assertEqual(self.inspect(job)["status"], "SUCCEEDED")
        self.assertEqual(ingestion.inspect_event(job["ingestion_id"], self.state)["status"], "COMPLETED")
        for task in ("quality_agent", "change_agent"):
            old = next(t for t in before["tasks"] if t["id"] == task)
            new = next(t for t in final["tasks"] if t["id"] == task)
            self.assertEqual(old["attempts"], new["attempts"])
        self.assertEqual(len(final["approvals"]), 2)
        report = (self.state / job["workflow_run_id"] / "report.html").read_text(encoding="utf-8")
        self.assertIn("Queue delivery", report)
        self.assertIn("SUCCEEDED", report)

    def test_failures_backoff_deadletter_and_replay_preserve_attempt_history(self):
        self.clock()
        job = self.enqueue(max_attempts=3)
        for attempt in range(1, 4):
            failed = self.work(fail_job=True)
            self.assert_identity(job, failed)
            self.assertEqual(failed["attempts"], attempt)
            self.assertEqual(len(failed["attempt_history"]), attempt)
            self.assertEqual(failed["status"], "DEAD_LETTER" if attempt == 3 else "QUEUED")
            self.assertEqual(failed["available_at"], store.stamp(self.now + 2 ** (attempt - 1)))
            self.assertIsNone(self.claim())
            self.ready(failed)
        self.assertEqual(len(queue.list_jobs(self.state, deadletters=True)), 1)
        self.assertIn("job_dead_lettered", [e["event"] for e in failed["history"]])
        replay = queue.replay(job["id"], self.state, max_attempts=2)
        self.assert_identity(job, replay)
        self.assertEqual((replay["attempts"], replay["budget_attempts"], replay["replay_count"]), (3, 0, 1))
        self.assertIsNone(replay["last_error"])
        paused = self.work()
        self.assertEqual((paused["status"], paused["attempts"], paused["budget_attempts"]), ("WAITING_FOR_APPROVAL", 4, 1))
        self.assertEqual(paused["attempt_history"][-1]["replay_number"], 1)
        approvals.decide(job["workflow_run_id"], "approved", "Aerol", state_dir=self.state)
        self.assertEqual(self.inspect(job)["status"], "SUCCEEDED")
        self.assertEqual(len(workflow.list_runs(self.state)), 1)

    def test_replay_refuses_non_deadletter_and_terminal_revision_limit(self):
        job = self.enqueue()
        with self.assertRaises(OpsCheckError):
            queue.replay(job["id"], self.state)
        self.work()
        for _ in range(3):
            approvals.decide(job["workflow_run_id"], "rejected", "Aerol", "Revise.", self.state)
        final = self.inspect(job)
        self.assertEqual(final["status"], "DEAD_LETTER")
        self.assertFalse(final["replayable"])
        with self.assertRaises(OpsCheckError):
            queue.replay(job["id"], self.state)
        self.assertEqual(len(approvals.inspect_run(job["workflow_run_id"], self.state)["approvals"]), 3)

    def test_expired_attempt_budget_exhaustion_deadletters(self):
        self.clock()
        job = self.enqueue(max_attempts=1)
        self.claim(lease_seconds=5)
        self.now += 6
        self.assertIsNone(self.claim())
        saved = self.inspect(job)
        self.assertEqual(saved["status"], "DEAD_LETTER")
        self.assertEqual(saved["last_error_type"], "LeaseExpired")

    def test_busy_run_defers_without_consuming_failure_budget(self):
        self.clock()
        job = self.enqueue(max_attempts=1)
        with workflow._run_lock(self.state, job["workflow_run_id"], []):
            deferred = self.work()
        self.assertEqual(deferred["status"], "QUEUED")
        self.assertTrue(deferred["deferred"])
        self.assertEqual(deferred["budget_attempts"], 0)
        self.assertEqual(deferred["attempt_history"][0]["outcome"], "DEFERRED")
        self.ready(deferred)
        self.assertEqual(self.work()["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(len(workflow.list_runs(self.state)), 1)

    def test_run_failure_retry_reuses_completed_specialist(self):
        self.clock()
        job = self.enqueue()
        with patch.object(workflow, "validate", side_effect=OpsCheckError("temporary I/O problem")):
            first = self.work()
        self.assertEqual(first["status"], "QUEUED")
        before = approvals.inspect_run(job["workflow_run_id"], self.state)
        self.ready(first)
        self.assertEqual(self.work()["status"], "WAITING_FOR_APPROVAL")
        after = approvals.inspect_run(job["workflow_run_id"], self.state)
        self.assertEqual(next(t["attempts"] for t in before["tasks"] if t["id"] == "change_agent"),
                         next(t["attempts"] for t in after["tasks"] if t["id"] == "change_agent"))

    def test_source_drift_failure_can_replay_same_reservation_after_restoration(self):
        self.clock()
        job = self.enqueue(max_attempts=1)
        source = self.inbox / "data/orders-messy.csv"
        original = source.read_bytes()
        source.write_bytes(original + b"\n")
        failed = self.work()
        self.assertEqual(failed["status"], "DEAD_LETTER")
        self.assertEqual(workflow.list_runs(self.state), [])
        source.write_bytes(original)
        queue.replay(job["id"], self.state)
        self.assertEqual(self.work()["status"], "WAITING_FOR_APPROVAL")

    def test_terminal_identity_failure_is_not_replayable(self):
        job = self.enqueue()
        with patch.object(worker, "_execute", side_effect=worker.TerminalDeliveryError("Immutable identity mismatch")):
            result = self.work()
        self.assertFalse(result["replayable"])
        self.assertEqual(result["status"], "DEAD_LETTER")
        with self.assertRaises(OpsCheckError):
            queue.replay(job["id"], self.state)

    def test_job_and_attempt_identity_constraints(self):
        job = self.enqueue()
        self.claim()
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection:
            for statement, params in (
                ("UPDATE queue_jobs SET workflow_run_id=? WHERE id=?", ("a" * 32, job["id"])),
                ("UPDATE queue_jobs SET ingestion_id=? WHERE id=?", ("evt_changed", job["id"])),
                ("UPDATE queue_attempts SET lease_token='replacement' WHERE job_id=?", (job["id"],)),
                ("UPDATE queue_jobs SET status='QUEUED' WHERE id=?", (job["id"],)),
                ("INSERT INTO queue_jobs(id,ingestion_id,workflow_run_id,status,max_attempts,available_at,created_at,updated_at) VALUES('other',?,?,'QUEUED',3,'x','x','x')", (job["ingestion_id"], "b" * 32))):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError), connection:
                    connection.execute(statement, params)

    def test_enqueue_reservation_and_job_creation_rollback_together(self):
        original = store.log
        def fail(connection, job_id, event, **detail):
            if event == "job_created":
                raise RuntimeError("before commit")
            return original(connection, job_id, event, **detail)
        with patch.object(store, "log", side_effect=fail), self.assertRaises(RuntimeError):
            self.enqueue()
        self.assertEqual(queue.list_jobs(self.state), [])
        self.assertIsNone(ingestion.list_events(self.state)[0]["workflow_run_id"])
        self.assertEqual(self.enqueue()["status"], "QUEUED")

    def test_ingest_claim_observed_before_enqueue_reuses_newly_saved_reservation(self):
        event = ingestion.receive(self.manifest, self.state)
        self.assertIsNone(event["workflow_run_id"])
        job = self.enqueue()
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection:
            reserved = ingestion._claim(connection, event, False)
        self.assertEqual(reserved, job["workflow_run_id"])
        saved = ingestion.inspect_event(event["id"], self.state)
        self.assertEqual(sum(e["event"] == "workflow_reserved" for e in saved["history"]), 1)

    def test_replay_of_failed_revision_retains_approval_history_without_source_files(self):
        job = self.enqueue(max_attempts=1)
        self.work()
        with patch.object(approvals, "revise_briefing", side_effect=ValueError("revision interrupted")):
            failed = approvals.decide(job["workflow_run_id"], "rejected", "Aerol", "Explain changes.", self.state)
        self.assertEqual(self.inspect(job)["status"], "DEAD_LETTER")
        for source in (self.inbox / "data").iterdir():
            source.unlink()
        queue.replay(job["id"], self.state)
        self.assertEqual(self.work()["status"], "WAITING_FOR_APPROVAL")
        recovered = approvals.inspect_run(job["workflow_run_id"], self.state)
        self.assertEqual(recovered["approvals"][0], failed["approvals"][0])
        self.assertEqual(len(recovered["approvals"]), 2)
        self.assertTrue(all(t["attempts"] == 1 for t in recovered["tasks"] if t["id"] in workflow.PLAN))

    def test_inspection_recovers_checkpoint_and_success_before_queue_commit(self):
        job = self.enqueue()
        claim = self.claim()
        worker._execute(claim, self.state)
        self.assertEqual(self.inspect(job)["status"], "WAITING_FOR_APPROVAL")
        with patch.object(approvals, "_publish_result", side_effect=RuntimeError("crash before publish")):
            with self.assertRaises(RuntimeError):
                approvals.decide(job["workflow_run_id"], "approved", "Aerol", state_dir=self.state)
        self.assertEqual(self.inspect(job)["status"], "SUCCEEDED")
        self.assertEqual(ingestion.inspect_event(job["ingestion_id"], self.state)["status"], "COMPLETED")
        self.assertEqual(len(approvals.inspect_run(job["workflow_run_id"], self.state)["approvals"]), 1)

    def test_heartbeat_thread_renews_and_closes_without_audit_spam(self):
        self.clock()
        self.enqueue()
        claim = self.claim()
        heartbeat = worker.Heartbeat(claim, self.state, 30)
        # Drive the thread's interval boundary without wall-clock sleeping.
        renew = queue.heartbeat
        def renew_later(*args, **kwargs):
            self.now += 10
            return renew(*args, **kwargs)
        with patch.object(heartbeat.stop, "wait", side_effect=[False, True]), patch.object(queue, "heartbeat", side_effect=renew_later) as renewal:
            with heartbeat:
                heartbeat.thread.join(timeout=10)
                self.assertFalse(heartbeat.thread.is_alive())
            self.assertEqual(renewal.call_count, 2)  # Initial ownership check + background renewal.
        self.assertFalse(heartbeat.lost.is_set())
        self.assertEqual(self.inspect(claim)["lease_expires_at"], store.stamp(self.now + 30))
        self.now += 11  # Beyond the original lease, still inside the renewed lease.
        self.assertIsNone(self.claim("other"))
        (self.state / "runs.sqlite3").unlink()

    def test_live_work_retains_run_lock_after_lease_loss_and_reclaimer_defers(self):
        self.clock()
        job = self.enqueue(max_attempts=2)
        first = self.claim(lease_seconds=5)
        entered, release = threading.Event(), threading.Event()
        original = workflow.validate
        def blocked(*args):
            entered.set()
            if not release.wait(15):
                raise RuntimeError("test barrier timed out")
            return original(*args)
        errors = []
        def process_first():
            try:
                worker.process(first, self.state, lease_seconds=5)
            except Exception as exc:
                errors.append(exc)
        # Disable renewal for this deliberately lost-lease case, not workflow locks.
        with patch.object(workflow, "validate", side_effect=blocked), patch.object(worker.Heartbeat, "_loop", lambda h: h.stop.wait()):
            thread = threading.Thread(target=process_first)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                self.now += 6
                reclaimed = self.claim("worker-b")
                self.assertIsNotNone(reclaimed)
                deferred = worker.process(reclaimed, self.state)
                self.assertTrue(deferred["deferred"])
                self.assertEqual(deferred["budget_attempts"], 1)
            finally:
                release.set()
                thread.join(15)
            self.assertFalse(thread.is_alive())
        self.assertTrue(any(isinstance(e, queue.LeaseLostError) for e in errors))
        self.assertEqual(self.inspect(job)["status"], "WAITING_FOR_APPROVAL")
        run = approvals.inspect_run(job["workflow_run_id"], self.state)
        self.assertEqual(next(t["attempts"] for t in run["tasks"] if t["id"] == "quality_agent"), 1)
        self.assertEqual(len(workflow.list_runs(self.state)), 1)

    def test_report_write_failure_keeps_authoritative_pause_and_returns_error(self):
        job = self.enqueue()
        with patch("opscheck.artifacts.write_workflow_reports", side_effect=OSError("report destination unavailable")):
            result = self.work()
        self.assertEqual(result["status"], "WAITING_FOR_APPROVAL")
        self.assertTrue(result["processing_error"])
        self.assertIn("delivery_warning", [e["event"] for e in result["history"]])
        self.assertIsNone(self.claim())
        self.assertEqual(len(workflow.list_runs(self.state)), 1)

    def test_queue_schema_initialization_failure_closes_connection(self):
        original = sqlite3.connect
        connections = []
        def connect(*args, **kwargs):
            result = original(*args, **kwargs)
            connections.append(result)
            return result
        with patch.object(workflow.sqlite3, "connect", side_effect=connect), patch.object(store, "initialize", side_effect=sqlite3.OperationalError("schema failure")):
            with self.assertRaises(OpsCheckError):
                self.enqueue()
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        (self.state / "runs.sqlite3").unlink()

    def test_heartbeat_loss_refuses_settlement_and_stops_thread(self):
        self.enqueue()
        claim = self.claim()
        original = worker.Heartbeat.__enter__
        def lose(heartbeat):
            result = original(heartbeat)
            heartbeat.lost.set()
            heartbeat.error = "controlled renewal failure"
            return result
        with patch.object(worker.Heartbeat, "__enter__", lose), patch.object(worker, "_execute", return_value={"status": "FAILED", "error": "x"}):
            with self.assertRaises(queue.LeaseLostError):
                worker.process(claim, self.state)
        self.assertFalse(any(t.name == "opscheck-heartbeat" for t in threading.enumerate()))
        self.assertEqual(self.inspect(claim)["status"], "LEASED")

    def test_keyboard_interrupt_requeues_and_stops_heartbeat(self):
        self.enqueue()
        claim = self.claim()
        with patch.object(worker, "_execute", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            worker.process(claim, self.state)
        self.assertEqual(self.inspect(claim)["status"], "QUEUED")
        self.assertFalse(any(t.name == "opscheck-heartbeat" for t in threading.enumerate()))

    def test_cli_validation_exit_codes_and_injection(self):
        for args in (("enqueue", self.manifest, "--max-attempts", "11"),
                     ("worker", "--once", "--worker-id", "bad\x1bname"),
                     ("worker", "--once", "--lease-seconds", "nan"),
                     ("worker", "--once", "--poll-interval", "0"),
                     ("worker", "--once", "--max-jobs", "0")):
            self.assertEqual(self.command(*args).returncode, 2)
        self.assertEqual(self.command("worker", "--once").returncode, 0)
        queued = self.command("enqueue", self.manifest, "--max-attempts", "1")
        self.assertEqual(queued.returncode, 0, queued.stderr)
        job = json.loads(queued.stdout)
        self.assertEqual(self.command("worker", "--once", "--fail-job").returncode, 2)
        self.assertEqual(json.loads(self.command("deadletters").stdout)["id"], job["id"])
        self.assertEqual(self.command("replay", job["id"]).returncode, 0)
        self.assertEqual(self.command("worker", "--once").returncode, 0)
        self.assertEqual(self.command("approve", job["workflow_run_id"], "--reviewer", "Aerol").returncode, 0)
        self.assertEqual(json.loads(self.command("job", job["id"]).stdout)["status"], "SUCCEEDED")
        self.assertEqual(self.command("replay", job["id"]).returncode, 2)

    def test_max_jobs_counts_claims_and_empty_loop_polls(self):
        self.enqueue()
        results = []
        self.assertEqual(worker.run(self.state, max_jobs=1, emit=results.append), 0)
        self.assertEqual(len(results), 1)
        with patch.object(worker.time, "sleep", side_effect=KeyboardInterrupt) as sleep:
            with self.assertRaises(KeyboardInterrupt):
                worker.run(self.state, poll_interval=.1)
        sleep.assert_called_once_with(.1)

    def test_fail_job_once_does_not_repeat_or_apply_after_replay(self):
        self.clock()
        self.enqueue()
        first = self.work(fail_job_once=True)
        self.assertEqual(first["status"], "QUEUED")
        self.ready(first)
        self.assertEqual(self.work(fail_job_once=True)["status"], "WAITING_FOR_APPROVAL")

    def test_metadata_html_and_cli_are_escaped(self):
        from opscheck.report import _queue_html
        hostile = '<script>alert("x")</script>'
        rendered = _queue_html({"queue": {"id": hostile, "status": hostile, "last_error": hostile}})
        self.assertIn(escape(hostile), rendered)
        self.assertNotIn("<script", rendered)
        for value in ("", " ", "x" * 129, "worker\nother", "\x7f", "\u202e"):
            with self.assertRaises(OpsCheckError):
                queue.worker_identity(value)

    def test_state_cannot_overwrite_manifest_or_sources(self):
        state = self.root / "unsafe"
        state.mkdir()
        source = state / "runs.sqlite3"
        source.write_text('order_id\n1\n', encoding="utf-8")
        original = source.read_bytes()
        manifest = state / "job.opscheck.json"
        manifest.write_text(json.dumps({"event_type": "orders_check", "input": "runs.sqlite3", "before": "runs.sqlite3",
                                        "after": "runs.sqlite3", "rules": "runs.sqlite3"}), encoding="utf-8")
        with self.assertRaises(OpsCheckError):
            queue.enqueue(manifest, state)
        self.assertEqual(source.read_bytes(), original)

    def test_empty_inspection_does_not_create_state(self):
        self.assertEqual(queue.list_jobs(self.state), [])
        self.assertIsNone(self.claim())
        for operation in (lambda: queue.inspect_job("missing", self.state), lambda: queue.replay("missing", self.state)):
            with self.assertRaises(OpsCheckError):
                operation()
        self.assertFalse(self.state.exists())

    def test_connections_close_after_all_delivery_paths(self):
        original = workflow._connect
        connections = []
        def connect(*args):
            result = original(*args)
            connections.append(result)
            return result
        with patch.object(workflow, "_connect", side_effect=connect), patch.object(ingestion, "_connect", side_effect=connect):
            job = self.enqueue(max_attempts=1)
            self.work(fail_job=True)
            queue.replay(job["id"], self.state)
            self.work()
            approvals.decide(job["workflow_run_id"], "approved", "Aerol", state_dir=self.state)
            self.inspect(job)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        (self.state / "runs.sqlite3").unlink()

    def subprocesses(self, script, count, *args):
        children = [subprocess.Popen([sys.executable, "-c", script, str(self.state), *map(str, args)], cwd=ROOT,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(count)]
        def cleanup():
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()
        self.addCleanup(cleanup)
        return children

    def test_real_two_worker_race_one_job_one_execution(self):
        job = self.enqueue()
        script = '''
import json, os, sys
from opscheck import worker
print("ready", flush=True)
sys.stdin.readline()
worker.run(sys.argv[1], once=True, worker_id="worker-"+str(os.getpid()), emit=lambda x: print(json.dumps(x)))
'''
        children = self.subprocesses(script, 2)
        for child in children:
            self.assertEqual(child.stdout.readline().strip(), "ready")
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.communicate(timeout=30) for child in children]
        self.assertEqual([p.returncode for p in children], [0, 0], results)
        statuses = sorted(json.loads(output)["status"] for output, _ in results)
        self.assertEqual(statuses, ["EMPTY", "WAITING_FOR_APPROVAL"])
        saved = self.inspect(job)
        self.assertEqual(saved["attempts"], 1)
        self.assertEqual(len(workflow.list_runs(self.state)), 1)
        self.assertEqual(len(saved["attempt_history"]), 1)

    def test_real_two_workers_can_hold_different_jobs_concurrently(self):
        first = self.enqueue()
        document = json.loads(self.manifest.read_text())
        document["key"] = "email"
        self.manifest.write_text(json.dumps(document), encoding="utf-8")
        second = self.enqueue()
        script = '''
import json, os, sys
from opscheck import queue, worker
job = queue.claim(sys.argv[1], worker_id="worker-"+str(os.getpid()))
print(json.dumps({"id":job["id"],"status":job["status"]}), flush=True)
sys.stdin.readline()
print(json.dumps(worker.process(job, sys.argv[1])))
'''
        # The first job's input content did not change; only the submission's key did.
        children = self.subprocesses(script, 2)
        leases = [json.loads(child.stdout.readline()) for child in children]
        self.assertEqual({r["id"] for r in leases}, {first["id"], second["id"]})
        self.assertEqual([r["status"] for r in leases], ["LEASED", "LEASED"])
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = [child.communicate(timeout=30) for child in children]
        self.assertEqual([p.returncode for p in children], [0, 0], results)
        self.assertEqual(len(workflow.list_runs(self.state)), 2)

    def test_real_worker_crashes_at_all_durable_boundaries(self):
        script = '''
import os, sys
from opscheck import queue, worker, workflow, approvals
job = queue.claim(sys.argv[1], worker_id="crashing-worker", lease_seconds=5)
boundary = sys.argv[2]
if boundary == "claim":
    os._exit(23)
if boundary == "worker":
    workflow.validate = lambda *a: os._exit(23)
if boundary == "waiting":
    queue.finish = lambda *a, **k: os._exit(23)
worker.process(job, sys.argv[1], lease_seconds=5)
if boundary == "success":
    approvals._publish_result = lambda *a: os._exit(23)
    approvals.decide(job["workflow_run_id"], "approved", "Crash QA", state_dir=sys.argv[1])
'''
        for boundary in ("claim", "worker", "waiting", "success"):
            with self.subTest(boundary=boundary):
                self.state = self.root / boundary
                job = self.enqueue()
                completed = subprocess.run([sys.executable, "-c", script, str(self.state), boundary], cwd=ROOT,
                                           capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 23, completed.stderr)
                saved_tasks = {}
                if workflow.list_runs(self.state):
                    saved_run = approvals.inspect_run(job["workflow_run_id"], self.state)
                    saved_tasks = {task["id"]: task for task in saved_run["tasks"]}
                if boundary in ("claim", "worker"):
                    # Advance the queue clock beyond persisted expiry, without a sleep.
                    saved = self.inspect(job)
                    future = datetime.fromisoformat(saved["lease_expires_at"]).timestamp() + 1
                    with patch.object(store, "clock", return_value=future):
                        recovered = self.work()
                    self.assert_identity(job, recovered)
                final = self.inspect(job)
                self.assertEqual(final["status"], "SUCCEEDED" if boundary == "success" else "WAITING_FOR_APPROVAL")
                self.assertEqual(len(workflow.list_runs(self.state)), 1)
                run = approvals.inspect_run(job["workflow_run_id"], self.state)
                self.assertEqual(len(run["approvals"]), 1)
                for name in ("quality_agent", "change_agent"):
                    before = saved_tasks.get(name, {"attempts": 0, "status": "pending"})
                    expected = before["attempts"] + int(before["status"] != "succeeded")
                    self.assertEqual(next(t["attempts"] for t in run["tasks"] if t["id"] == name), expected)


if __name__ == "__main__":
    unittest.main()
