"""Event identity, process coordination, recovery, and approval integration tests."""
from contextlib import closing
from html import escape
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from opscheck import approvals, event_store, ingestion, manifests, workflow
from opscheck.core import OpsCheckError

ROOT = Path(__file__).resolve().parents[1]


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.data = self.inbox / "data"
        self.data.mkdir(parents=True)
        self.state = self.root / "state"
        (self.data / "orders.csv").write_text("order_id,alt,email\n1,A,invalid\n2,B,b@example.com\n", encoding="utf-8")
        (self.data / "before.csv").write_text("order_id,alt,email\n1,A,a@example.com\n", encoding="utf-8")
        (self.data / "after.csv").write_text("order_id,alt,email\n1,A,new@example.com\n2,B,b@example.com\n", encoding="utf-8")
        (self.data / "rules.json").write_text(json.dumps({"version": 1, "columns": {"email": {"type": "email"}}}), encoding="utf-8")
        self.document = {"event_type": "orders_check", "input": "data/orders.csv", "rules": "data/rules.json",
                         "before": "data/before.csv", "after": "data/after.csv"}
        self.job = self.write_job("job.opscheck.json", self.document)

    def write_job(self, name, document):
        path = self.inbox / name
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        return path

    def ingest(self, path=None):
        return ingestion.ingest(path or self.job, self.state)

    def events(self):
        return ingestion.list_events(self.state)

    def runs(self):
        return workflow.list_runs(self.state)

    def read(self, event):
        return ingestion.inspect_event(event["id"], self.state)

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "opscheck", *map(str, args), "--state-dir", str(self.state)],
                              cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                              capture_output=True, encoding="utf-8", timeout=30)

    def test_valid_manifest_persists_lifecycle_and_expected_identity(self):
        event = self.ingest()
        self.assertEqual(event["status"], "WAITING_FOR_APPROVAL")
        self.assertTrue(event["workflow_created"])
        self.assertFalse(event["duplicate"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.runs()), 1)
        config = event["workflow_config"]
        self.assertEqual(config["key"], "order_id")
        self.assertEqual(config["input_paths"][0], str((self.data / "orders.csv").resolve()))
        self.assertEqual(event["expected_workflow_fingerprint"], workflow._fingerprint(
            [Path(path) for path in config["input_paths"]], config))
        self.assertEqual([e["event"] for e in event["history"]],
                         ["event_received", "event_validated", "event_claimed", "workflow_reserved",
                          "workflow_created", "event_waiting_for_approval"])
        self.assertIsNotNone(event["claimed_at"])
        self.assertIsNone(event["completed_at"])

    def test_manifest_schema_errors_are_durable_and_never_run(self):
        cases = [({k: v for k, v in self.document.items() if k != "before"}, "Required field"),
                 ({**self.document, "input": 123}, "nonblank string"),
                 ({**self.document, "event_type": "execute_python"}, "Unsupported event_type"),
                 ({**self.document, "key": " "}, "nonblank string"),
                 ({**self.document, "key": []}, "nonblank string"),
                 ({**self.document, "event_id": None}, "nonblank string"),
                 ({**self.document, "module": "os.system"}, "Unsupported manifest field"),
                 ({**self.document, "after": "absent.csv"}, "does not exist"),
                 ({**self.document, "before": "data"}, "not a regular file"),
                 ({**self.document, "key": "\nkey"}, "Invalid key"),
                 ({**self.document, "event_id": "a" * 257}, "Invalid event_id")]
        # Embedded control characters cannot be normalized into valid processing keys.
        cases[-2] = ({**self.document, "key": "ke\ny"}, "Invalid key")
        for i, (document, error) in enumerate(cases):
            with self.subTest(document=document):
                result = self.ingest(self.write_job(f"invalid-{i}.opscheck.json", document))
                self.assertEqual(result["status"], "INVALID")
                self.assertIn(error, result["error"])
                self.assertIsNone(result["workflow_run_id"])
                self.assertEqual(self.read(result)["error"], result["error"])
        self.assertEqual(len(self.runs()), 0)

    def test_bad_json_duplicate_keys_and_nonstandard_constants(self):
        for text in ("{broken", "[]", '{"event_type":"orders_check","event_type":"other"}', '{"key":NaN}'):
            with self.subTest(text=text):
                self.job.write_text(text, encoding="utf-8")
                event = self.ingest()
                self.assertEqual(event["status"], "INVALID")
                self.assertIsNone(event["workflow_run_id"])
        self.assertEqual(len(self.runs()), 0)

    def test_invalid_replay_is_bounded_and_corrected_manifest_can_run(self):
        self.job.write_text("not JSON", encoding="utf-8")
        first, second = self.ingest(), self.ingest()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(second["duplicate_count"], 1)
        self.write_job(self.job.name, self.document)
        valid = self.ingest()
        self.assertEqual(valid["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(len(self.events()), 2)

    def test_missing_unreadable_or_oversized_manifest_is_inspectable(self):
        for path in (self.inbox / "missing.json", self.data):
            with self.subTest(path=path):
                self.assertEqual(self.ingest(path)["status"], "INVALID")
        self.job.write_bytes(b" " * (manifests.MAX_MANIFEST_BYTES + 1))
        self.assertIn("64 KiB", self.ingest()["error"])
        self.job.write_bytes(b"\xff")
        self.assertEqual(self.ingest()["status"], "INVALID")

    def test_relative_paths_cannot_escape_or_use_absolute_paths(self):
        outside = self.root / "outside.csv"
        outside.write_text("value\n1\n", encoding="utf-8")
        for value in ("../outside.csv", str(outside), "C:\\secret.csv", "C:secret.csv", "data/file:stream", "\\\\host\\share\\file"):
            with self.subTest(path=value):
                event = self.ingest(self.write_job("bad.opscheck.json", {**self.document, "input": value}))
                self.assertEqual(event["status"], "INVALID")
        self.assertFalse(self.runs())

    def test_windows_separators_and_normalized_relative_paths_deduplicate(self):
        first = self.ingest()
        copied = self.write_job("copy.json", {**self.document, "key": " order_id ", "input": "data\\orders.csv"})
        second = self.ingest(copied)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["duplicate"])

    @unittest.skipUnless(os.name == "posix", "symlink creation on Windows may require privileges")
    def test_source_symlink_cannot_escape_manifest_directory(self):
        outside = self.root / "outside.csv"
        outside.write_text("value\n1\n", encoding="utf-8")
        (self.data / "alias.csv").symlink_to(outside)
        event = self.ingest(self.write_job("bad.json", {**self.document, "input": "data/alias.csv"}))
        self.assertEqual(event["status"], "INVALID")
        self.assertIn("escapes", event["error"])

    def test_same_manifest_and_renamed_copy_create_one_run(self):
        first, second = self.ingest(), self.ingest()
        copy = self.inbox / "copy.opscheck.json"
        shutil.copyfile(self.job, copy)
        third = self.ingest(copy)
        self.assertEqual({e["id"] for e in (first, second, third)}, {first["id"]})
        self.assertEqual({e["workflow_run_id"] for e in (first, second, third)}, {first["workflow_run_id"]})
        self.assertEqual(third["duplicate_count"], 2)
        self.assertEqual(len(self.runs()), 1)
        self.assertFalse(third["workflow_created"])
        run = approvals.inspect_run(first["workflow_run_id"], self.state)
        self.assertTrue(all(t["attempts"] == 1 for t in run["tasks"]))

    def test_copied_data_directory_and_reformatted_json_have_same_identity(self):
        first = self.ingest()
        clone = self.root / "copy"
        shutil.copytree(self.inbox, clone)
        (clone / self.job.name).write_text(json.dumps(dict(reversed(list(self.document.items())))), encoding="utf-8")
        second = self.ingest(clone / self.job.name)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["workflow_run_id"], second["workflow_run_id"])
        self.assertEqual(len(self.runs()), 1)

    def test_explicit_id_replay_and_conflict_do_not_create_second_workflow(self):
        self.write_job(self.job.name, {**self.document, "event_id": "import-123"})
        first, replay = self.ingest(), self.ingest()
        self.assertEqual(first["id"], replay["id"])
        self.write_job(self.job.name, {**self.document, "event_id": "import-123", "key": "alt"})
        conflict = self.ingest()
        self.assertEqual(conflict["status"], "INVALID")
        self.assertIn("already used with different content", conflict["error"])
        self.assertIsNone(conflict["workflow_run_id"])
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.read(first)["fingerprint"], first["fingerprint"])

    def test_external_ids_are_aliases_of_canonical_content_and_each_is_bound(self):
        first = self.ingest()
        for external in ("customer-a", "customer-b"):
            self.write_job(self.job.name, {**self.document, "event_id": external})
            self.assertEqual(self.ingest()["id"], first["id"])
        self.assertEqual(self.read(first)["external_event_ids"], ["customer-a", "customer-b"])
        self.write_job(self.job.name, {**self.document, "event_id": "customer-b", "key": "alt"})
        self.assertEqual(self.ingest()["status"], "INVALID")
        self.assertEqual(len(self.runs()), 1)

    def test_changed_processing_key_creates_new_canonical_run(self):
        first = self.ingest()
        second = self.ingest(self.write_job("key.opscheck.json", {**self.document, "key": "alt"}))
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["workflow_run_id"], second["workflow_run_id"])
        self.assertEqual(second["status"], "WAITING_FOR_APPROVAL")

    def test_changed_rule_contents_create_new_canonical_run(self):
        first = self.ingest()
        (self.data / "rules.json").write_text(json.dumps({"version": 1, "columns": {"order_id": {"required": True}}}), encoding="utf-8")
        second = self.ingest()
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(len(self.runs()), 2)

    def test_changed_source_contents_create_new_fingerprint(self):
        first = self.ingest()
        with (self.data / "orders.csv").open("a", encoding="utf-8") as stream:
            stream.write("3,C,c@example.com\n")
        second = self.ingest()
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(len(self.runs()), 2)

    def test_scan_is_sorted_isolates_invalid_and_deduplicates_repeated_scans(self):
        self.job.rename(self.inbox / "b.opscheck.json")
        self.write_job("a.opscheck.json", {**self.document, "key": "alt"})
        self.write_job("c.opscheck.json", self.document)
        self.write_job("d.opscheck.json", {"event_type": "unknown"})
        self.write_job("ignored.json", {"not": "a job"})
        first = ingestion.scan(self.inbox, self.state)
        self.assertEqual([Path(e["manifest_path"]).name for e in first["events"]],
                         ["a.opscheck.json", "b.opscheck.json", "b.opscheck.json", "d.opscheck.json"])
        # Canonical event records retain the first manifest path; duplicate audit
        # entries retain the c.opscheck.json submission path.
        self.assertEqual({k: first[k] for k in ("scanned", "new_events", "duplicates", "invalid", "workflows_created")},
                         {"scanned": 4, "new_events": 2, "duplicates": 1, "invalid": 1, "workflows_created": 2})
        second = ingestion.scan(self.inbox, self.state)
        self.assertEqual((second["new_events"], second["duplicates"], second["invalid"], second["workflows_created"]), (0, 3, 1, 0))
        self.assertEqual(len(self.runs()), 2)

    def test_scan_io_failure_does_not_stop_other_manifests(self):
        second = self.write_job("z.opscheck.json", self.document)
        original = ingestion.ingest
        def fail_first(path, state_dir):
            if Path(path) == self.job:
                raise OpsCheckError("access denied")
            return original(path, state_dir)
        with patch.object(ingestion, "ingest", side_effect=fail_first):
            result = ingestion.scan(self.inbox, self.state)
        self.assertEqual((result["failed"], result["workflows_created"]), (1, 1))
        self.assertEqual(result["events"][1]["manifest_path"], str(second))

    def test_approval_rejection_and_completion_synchronize_same_event(self):
        event = self.ingest()
        run_id = event["workflow_run_id"]
        revised = approvals.decide(run_id, "rejected", reviewer="Aerol", comment="Explain changed records", state_dir=self.state)
        self.assertEqual(self.read(event)["status"], "WAITING_FOR_APPROVAL")
        final = approvals.decide(run_id, "approved", reviewer="Aerol", state_dir=self.state)
        saved = self.read(event)
        self.assertEqual(saved["status"], "COMPLETED")
        self.assertIsNotNone(saved["completed_at"])
        self.assertEqual(saved["workflow_run_id"], run_id)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(final["approvals"][0], revised["approvals"][0])
        self.assertEqual(sum(e["event"] == "event_completed" for e in saved["history"]), 1)
        self.assertEqual(self.read(event)["history"], saved["history"])
        self.assertEqual(final["ingestion"]["status"], "COMPLETED")
        html = Path(final["report_path"]).read_text(encoding="utf-8")
        self.assertIn(event["id"], html)
        self.assertIn("Event ingestion", html)

    def test_inspection_recovers_status_after_crash_before_ingestion_sync(self):
        event = self.ingest()
        # Simulate approval committed before report/synchronization runs.
        with patch.object(approvals, "_publish_result", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                approvals.decide(event["workflow_run_id"], "approved", state_dir=self.state)
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT status FROM ingestion_events").fetchone()[0], "WAITING_FOR_APPROVAL")
        run = approvals.inspect_run(event["workflow_run_id"], self.state)
        self.assertEqual(run["ingestion"]["status"], "COMPLETED")
        self.assertEqual(self.read(event)["status"], "COMPLETED")

    def test_synchronization_locks_before_reading_authoritative_state(self):
        event = self.ingest()
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection:
            statements = []
            connection.set_trace_callback(statements.append)
            with connection:
                event_store.synchronize(connection, event["workflow_run_id"])
                self.assertTrue(connection.in_transaction)
            self.assertEqual(statements[0], "BEGIN IMMEDIATE")
            self.assertFalse(connection.in_transaction)
        self.assertEqual(sum(e["event"] == "event_waiting_for_approval" for e in self.read(event)["history"]), 1)

    def test_source_change_between_receipt_and_execution_fails_without_silent_rehash(self):
        source = self.data / "orders.csv"
        original = source.read_bytes()
        execute = ingestion._execute
        def changed(event, run_id, directory):
            source.write_bytes(original + b"3,C,c@example.com\n")
            return execute(event, run_id, directory)
        with patch.object(ingestion, "_execute", side_effect=changed):
            failed = self.ingest()
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("changed after event receipt", failed["error"])
        self.assertEqual(len(self.runs()), 0)
        with self.assertRaises(OpsCheckError):
            workflow.run_workflow(*failed["workflow_config"]["input_paths"], state_dir=self.state,
                                  run_id=failed["workflow_run_id"])
        source.write_bytes(original)
        recovered = ingestion.retry_event(failed["id"], self.state)
        self.assertEqual(recovered["status"], "WAITING_FOR_APPROVAL")
        self.assertEqual(recovered["workflow_run_id"], failed["workflow_run_id"])
        self.assertEqual(recovered["fingerprint"], failed["fingerprint"])

    def test_source_changes_during_workers_are_rejected_by_existing_verifier(self):
        original = workflow.validate
        def mutate(*args, **kwargs):
            result = original(*args, **kwargs)
            (self.data / "orders.csv").write_text("order_id,alt,email\n3,C,other@example.com\n", encoding="utf-8")
            return result
        with patch.object(workflow, "validate", side_effect=mutate):
            failed = self.ingest()
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("changed during", failed["error"])
        self.assertFalse(approvals.inspect_run(failed["workflow_run_id"], self.state)["approvals"])

    def test_report_failure_is_reported_without_undoing_saved_workflow(self):
        from io import StringIO
        from opscheck.cli import main
        output = StringIO()
        with patch("opscheck.artifacts._atomic_write", side_effect=PermissionError("report locked")), patch("sys.stdout", output):
            code = main(["ingest", str(self.job), "--state-dir", str(self.state)])
        self.assertEqual(code, 2)
        self.assertIn("Processing error saved in audit history", output.getvalue())
        event = self.events()[0]
        self.assertEqual(event["status"], "WAITING_FOR_APPROVAL")
        self.assertTrue(any(e["event"] == "event_failed" and "report locked" in e["error"] for e in event["history"]))
        resumed = approvals.resume_run(event["workflow_run_id"], self.state)
        self.assertTrue(Path(resumed["report_path"]).is_file())
        self.assertEqual(len(resumed["approvals"]), 1)
        self.assertEqual(len(self.runs()), 1)

    def test_recoverable_precreation_failure_retries_same_reservation(self):
        with patch.object(ingestion, "_execute", side_effect=OSError("temporary failure")):
            event = self.ingest()
        self.assertEqual(event["status"], "FAILED")
        self.assertTrue(event["retryable"])
        self.assertEqual(len(self.runs()), 0)
        # Replays do not silently retry a failed job on every scan.
        self.assertEqual(self.ingest()["status"], "FAILED")
        recovered = ingestion.retry_event(event["id"], self.state)
        self.assertEqual(recovered["workflow_run_id"], event["workflow_run_id"])
        self.assertEqual(len(self.runs()), 1)
        self.assertIn("event_retried", [entry["event"] for entry in recovered["history"]])

    def test_failed_workflow_retry_reuses_completed_specialist(self):
        def injected(event, run_id, directory):
            config = event["workflow_config"]
            return workflow.run_workflow(*config["input_paths"], state_dir=directory,
                reserved_run_id=run_id, expected_fingerprint=event["expected_workflow_fingerprint"],
                fail_once="quality_agent", max_attempts=1)
        with patch.object(ingestion, "_execute", side_effect=injected):
            failed = self.ingest()
        self.assertEqual(failed["status"], "FAILED")
        recovered = ingestion.retry_event(failed["id"], self.state)
        self.assertEqual(recovered["status"], "WAITING_FOR_APPROVAL")
        run = approvals.inspect_run(recovered["workflow_run_id"], self.state)
        self.assertEqual({t["id"]: t["attempts"] for t in run["tasks"]}["change_agent"], 1)
        self.assertEqual(len(self.runs()), 1)

    def test_event_retry_preserves_revision_history_when_sources_have_moved(self):
        event = self.ingest()
        with patch.object(approvals, "revise_briefing", side_effect=ValueError("interrupted revision")):
            failed = approvals.decide(event["workflow_run_id"], "rejected", comment="Explain changes", state_dir=self.state)
        self.assertEqual(self.read(event)["status"], "FAILED")
        for path in self.data.iterdir():
            path.unlink()
        recovered = ingestion.retry_event(event["id"], self.state)
        self.assertEqual(recovered["status"], "WAITING_FOR_APPROVAL")
        run = approvals.inspect_run(event["workflow_run_id"], self.state)
        self.assertEqual(run["approvals"][0], failed["approvals"][0])
        self.assertEqual(len(run["approvals"]), 2)
        self.assertTrue(all(t["attempts"] == 1 for t in run["tasks"] if t["id"] in workflow.PLAN))

    def test_completed_waiting_and_invalid_events_cannot_retry(self):
        waiting = self.ingest()
        with self.assertRaisesRegex(OpsCheckError, "not retryable"):
            ingestion.retry_event(waiting["id"], self.state)
        approvals.decide(waiting["workflow_run_id"], "approved", state_dir=self.state)
        with self.assertRaisesRegex(OpsCheckError, "not retryable"):
            ingestion.retry_event(waiting["id"], self.state)
        invalid = self.ingest(self.write_job("invalid.json", {}))
        with self.assertRaisesRegex(OpsCheckError, "not retryable"):
            ingestion.retry_event(invalid["id"], self.state)
        self.assertEqual(len(self.runs()), 1)

    def test_revision_limit_is_terminal_for_event_retry(self):
        event = self.ingest()
        for _ in range(3):
            approvals.decide(event["workflow_run_id"], "rejected", comment="Clarify", state_dir=self.state)
        final = self.read(event)
        self.assertEqual(final["status"], "FAILED")
        self.assertFalse(final["retryable"])
        with self.assertRaisesRegex(OpsCheckError, "not retryable"):
            ingestion.retry_event(event["id"], self.state)
        self.assertEqual(len(approvals.inspect_run(event["workflow_run_id"], self.state)["approvals"]), 3)

    def test_sqlite_uniqueness_and_reservation_immutability(self):
        event = self.ingest()
        with closing(workflow._connect(self.state / "runs.sqlite3")) as connection:
            statements = [("UPDATE ingestion_events SET workflow_run_id=? WHERE id=?", ("a" * 32, event["id"])),
                          ("UPDATE ingestion_events SET fingerprint=? WHERE id=?", ("b" * 64, event["id"])),
                          ("INSERT INTO external_event_ids VALUES(?,?,?)", ("id", event["id"], event["fingerprint"]))]
            with connection:
                connection.execute(*statements[-1])
            for statement, params in statements:
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError), connection:
                    connection.execute(statement, params)

    def test_claim_and_reservation_transaction_roll_back_together(self):
        original = event_store.log
        def fail_reservation(connection, event_id, name, **detail):
            if name == "workflow_reserved":
                raise RuntimeError("crash before commit")
            return original(connection, event_id, name, **detail)
        with patch.object(event_store, "log", side_effect=fail_reservation):
            with self.assertRaises(RuntimeError):
                self.ingest()
        saved = self.events()[0]
        self.assertEqual(saved["status"], "VALIDATED")
        self.assertIsNone(saved["workflow_run_id"])
        self.assertEqual(saved["claim_attempts"], 0)
        self.assertEqual([e["event"] for e in saved["history"]], ["event_received", "event_validated"])
        self.assertEqual(self.ingest()["status"], "WAITING_FOR_APPROVAL")

    def test_real_process_crashes_recover_one_canonical_run(self):
        script = '''
import os, sys
from opscheck import ingestion, workflow
boundary = sys.argv[3]
if boundary == "persist":
    original = ingestion._persist
    def crash(*args):
        original(*args)
        os._exit(23)
    ingestion._persist = crash
elif boundary in ("claim", "reservation"):
    original = ingestion._claim
    def crash(*args):
        original(*args)
        os._exit(23)
    ingestion._claim = crash
elif boundary == "workflow":
    workflow._run_tasks = lambda *args: os._exit(23)
elif boundary == "worker":
    workflow.validate = lambda *args: os._exit(23)
elif boundary == "waiting":
    workflow._publish_result = lambda *args: os._exit(23)
ingestion.ingest(sys.argv[1], sys.argv[2])
'''
        for boundary in ("persist", "claim", "reservation", "workflow", "worker", "waiting"):
            with self.subTest(boundary=boundary):
                state = self.root / boundary
                completed = subprocess.run([sys.executable, "-c", script, str(self.job), str(state), boundary],
                                           cwd=ROOT, capture_output=True, text=True, timeout=30)
                self.assertEqual(completed.returncode, 23, completed.stderr)
                saved = ingestion.list_events(state)[0]
                reserved = saved["workflow_run_id"]
                recovered = ingestion.ingest(self.job, state)
                self.assertEqual(recovered["status"], "WAITING_FOR_APPROVAL")
                if reserved:
                    self.assertEqual(recovered["workflow_run_id"], reserved)
                self.assertEqual(len(ingestion.list_events(state)), 1)
                self.assertEqual(len(workflow.list_runs(state)), 1)
                self.assertEqual(sum(e["event"] == "workflow_reserved" for e in recovered["history"]), 1)
                self.assertEqual(len(approvals.inspect_run(recovered["workflow_run_id"], state)["approvals"]), 1)

    def test_two_processes_ingest_simultaneously_create_one_canonical_workflow(self):
        script = '''
import json, sys
from opscheck.ingestion import ingest
print("ready", flush=True)
sys.stdin.readline()
event = ingest(sys.argv[1], sys.argv[2])
print(json.dumps({k:event[k] for k in ("id","workflow_run_id","status","duplicate")}))
'''
        children = [subprocess.Popen([sys.executable, "-c", script, str(self.job), str(self.state)], cwd=ROOT,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            for child in children:
                self.assertEqual(child.stdout.readline().strip(), "ready")
            for child in children:
                child.stdin.write("go\n")
                child.stdin.flush()
            results = [child.communicate(timeout=30) for child in children]
            self.assertEqual([child.returncode for child in children], [0, 0], results)
            records = [json.loads(result[0]) for result in results]
            self.assertEqual(len({record["id"] for record in records}), 1)
            self.assertEqual(sum(record["duplicate"] for record in records), 1)
            self.assertEqual(len(self.events()), 1)
            self.assertEqual(len(self.runs()), 1)
            event = self.events()[0]
            self.assertEqual(event["status"], "WAITING_FOR_APPROVAL")
            self.assertEqual(event["claim_attempts"], 1)
            self.assertEqual(event["duplicate_count"], 1)
            self.assertEqual(sum(e["event"] == "workflow_reserved" for e in event["history"]), 1)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_state_cannot_overwrite_manifest_or_source_even_when_invalid(self):
        self.state.mkdir()
        database = self.state / "runs.sqlite3"
        database.write_bytes(self.job.read_bytes())
        original = database.read_bytes()
        with self.assertRaisesRegex(OpsCheckError, "overwrite an input"):
            self.ingest(database)
        self.assertEqual(database.read_bytes(), original)
        database.unlink()
        shutil.copyfile(self.data / "orders.csv", database)
        original = database.read_bytes()
        self.write_job(self.job.name, {**self.document, "input": str(database), "event_type": "invalid"})
        with self.assertRaisesRegex(OpsCheckError, "overwrite an input"):
            self.ingest()
        self.assertEqual(database.read_bytes(), original)

    def test_sqlite_hardlink_is_rejected_without_touching_source(self):
        self.state.mkdir()
        source = self.data / "orders.csv"
        original = source.read_bytes()
        os.link(source, self.state / "runs.sqlite3")
        with self.assertRaisesRegex(OpsCheckError, "hardlinks"):
            self.ingest()
        self.assertEqual(source.read_bytes(), original)

    def test_html_and_cli_escape_external_identity(self):
        attack = '<script>alert("x")</script>'
        self.write_job(self.job.name, {**self.document, "event_id": attack})
        event = self.ingest()
        report = self.state / event["workflow_run_id"] / "report.html"
        text = report.read_text(encoding="utf-8")
        self.assertIn(escape(attack), text)
        self.assertNotIn("<script", text)
        command = self.command("events")
        self.assertEqual(json.loads(command.stdout)["event_id"], attack)
        bad = self.write_job("bad.json", {**self.document, "event_id": "bad\x1b[31m\nforged"})
        command = self.command("ingest", bad)
        self.assertEqual(command.returncode, 2)
        self.assertNotIn("\x1b", command.stdout + command.stderr)
        self.assertFalse(any(line == "forged" for line in command.stdout.splitlines()))

    def test_cli_ingest_duplicate_scan_approve_and_event(self):
        first = self.command("ingest", self.job)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("WAITING_FOR_APPROVAL", first.stdout)
        self.assertIn("Duplicate event detected", self.command("ingest", self.job).stdout)
        event = self.events()[0]
        self.assertEqual(self.command("events").returncode, 0)
        self.assertIn(event["fingerprint"], self.command("event", event["id"]).stdout)
        self.assertIn("Workflows created: 0", self.command("scan", self.inbox).stdout)
        self.assertEqual(self.command("approve", event["workflow_run_id"], "--reviewer", "Aerol").returncode, 0)
        self.assertIn("Status: COMPLETED", self.command("event", event["id"]).stdout)
        self.assertEqual(self.command("retry-event", event["id"]).returncode, 2)
        self.write_job("invalid.opscheck.json", {})
        self.assertEqual(self.command("scan", self.inbox).returncode, 2)

    def test_readonly_empty_and_missing_event_operations_do_not_create_state(self):
        self.assertEqual(self.events(), [])
        with self.assertRaisesRegex(OpsCheckError, "Event not found"):
            ingestion.inspect_event("missing", self.state)
        with self.assertRaisesRegex(OpsCheckError, "Event not found"):
            ingestion.retry_event("missing", self.state)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.command("events").returncode, 0)
        self.assertEqual(self.command("event", "missing").returncode, 2)
        self.assertEqual(self.command("scan", self.root / "absent").returncode, 2)

    def test_connections_close_on_all_ingestion_paths_and_database_can_be_deleted(self):
        connections = []
        connect = sqlite3.connect
        def tracked(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            self.addCleanup(connection.close)
            return connection
        with patch.object(workflow.sqlite3, "connect", side_effect=tracked):
            event = self.ingest()
            self.ingest()
            self.read(event)
            self.events()
            self.ingest(self.write_job("bad.json", {}))
            with self.assertRaises(OpsCheckError):
                ingestion.retry_event(event["id"], self.state)
            approvals.decide(event["workflow_run_id"], "approved", state_dir=self.state)
        for connection in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        (self.state / "runs.sqlite3").unlink()


if __name__ == "__main__":
    unittest.main()
