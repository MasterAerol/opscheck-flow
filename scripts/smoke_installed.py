"""Exercise the installed package from a temporary directory, never the checkout."""
from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> None:
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="opscheck-installed-") as directory:
        def command(*args: str, expected: int = 0) -> str:
            completed = subprocess.run([sys.executable, "-m", "opscheck", *args], cwd=directory,
                                       env=environment, capture_output=True, encoding="utf-8", timeout=30)
            if completed.returncode != expected:
                raise RuntimeError(f"{args}: {completed.stdout}\n{completed.stderr}")
            return completed.stdout

        print(command("--version").strip())
        location = subprocess.run([sys.executable, "-c", "import opscheck; print(opscheck.__file__)"],
                                  cwd=directory, env=environment, capture_output=True, text=True, check=True)
        print(f"Installed module: {location.stdout.strip()}")
        assert "WAITING_FOR_APPROVAL" in command("flow-demo", "--fail-once", "quality_agent")
        report = next(Path(directory).glob(".opscheck/runs/*/report.json"))
        initial = json.loads(report.read_text(encoding="utf-8"))
        run_id = initial["run_id"]
        assert "WAITING_FOR_APPROVAL" in command("run", run_id)
        feedback = 'Explain changed orders. <script>alert("x")</script>'
        assert "revision_agent completed" in command("reject", run_id, "--reviewer", "Package QA", "--comment", feedback)
        history = command("approvals", run_id)
        assert "#1 REJECTED" in history and "#2 PENDING" in history
        assert "SUCCEEDED" in command("approve", run_id, "--reviewer", "Package QA")
        final = json.loads(report.read_text(encoding="utf-8"))
        original_attempts = {t["id"]: t["attempts"] for t in initial["tasks"]}
        final_attempts = {t["id"]: t["attempts"] for t in final["tasks"]}
        assert original_attempts["quality_agent"] == final_attempts["quality_agent"] == 2
        assert original_attempts["change_agent"] == final_attempts["change_agent"] == 1
        assert [a["status"] for a in final["approvals"]] == ["rejected", "approved"]
        html = report.with_suffix(".html").read_text(encoding="utf-8")
        assert "Approval History" in html and escape(feedback) in html and "<script" not in html
        print("PASS: installed retry -> pause -> reject -> revise -> approve; immutable history and escaped HTML.")

        # Obtain fixtures through the installed package resource API in a fresh
        # subprocess. No repository-relative paths participate in this smoke test.
        prepare = '''
from importlib import resources
from pathlib import Path
source = resources.files("opscheck").joinpath("examples", "inbox")
inbox = Path("inbox")
(inbox / "data").mkdir(parents=True)
(inbox / "job.opscheck.json").write_bytes(source.joinpath("job-001.opscheck.json").read_bytes())
for name in ("orders-messy.csv", "orders-before.csv", "orders-after.csv", "rules.json"):
    (inbox / "data" / name).write_bytes(source.joinpath("data", name).read_bytes())
'''
        subprocess.run([sys.executable, "-c", prepare], cwd=directory, env=environment, check=True)
        assert "WAITING_FOR_APPROVAL" in command("ingest", "inbox/job.opscheck.json")
        event = json.loads(command("events"))
        event_run = event["workflow_run_id"]
        assert "Duplicate event detected" in command("ingest", "inbox/job.opscheck.json")
        replay = json.loads(command("events"))
        assert replay["id"] == event["id"] and replay["workflow_run_id"] == event_run
        assert replay["duplicate_count"] == 1
        assert "SUCCEEDED" in command("approve", event_run, "--reviewer", "Package QA")
        assert "Status: COMPLETED" in command("event", event["id"])
        assert "Workflows created: 0" in command("scan", "inbox")
        print("PASS: installed event ingest -> duplicate/same run -> approve -> event COMPLETED; rescan creates zero workflows.")

        # A separate store proves enqueue itself runs no workflow or specialist.
        state = ("--state-dir", "queue-state")
        queued = json.loads(command("enqueue", "inbox/job.opscheck.json", *state))
        assert queued["status"] == "QUEUED" and queued["attempts"] == 0
        assert command("runs", *state).strip() == ""
        duplicate = json.loads(command("enqueue", "inbox/job.opscheck.json", *state))
        assert duplicate["id"] == queued["id"] and duplicate["workflow_run_id"] == queued["workflow_run_id"]
        assert not duplicate["job_created"]
        failed = json.loads(command("worker", "--once", "--fail-job-once", *state, expected=2))
        assert failed["status"] == "QUEUED" and failed["budget_attempts"] == 1
        # The bounded worker polls through the real one-second retry backoff.
        paused = json.loads(command("worker", "--max-jobs", "1", "--poll-interval", "0.1", *state))
        assert paused["status"] == "WAITING_FOR_APPROVAL" and paused["attempts"] == 2
        assert paused["workflow_run_id"] == queued["workflow_run_id"]
        assert "SUCCEEDED" in command("approve", queued["workflow_run_id"], "--reviewer", "Package QA", *state)
        final_job = json.loads(command("job", queued["id"], *state))
        assert final_job["status"] == "SUCCEEDED" and len(final_job["attempt_history"]) == 2
        assert "Status: COMPLETED" in command("event", queued["ingestion_id"], *state)
        assert "SUCCEEDED" in command("run", queued["workflow_run_id"], *state)
        assert "New queue jobs: 0" in command("scan", "inbox", "--enqueue", *state)
        print("PASS: installed enqueue/no execution -> duplicate/same job -> delivery failure/backoff -> worker -> approval -> queue SUCCEEDED/event COMPLETED.")

        once_state = ("--state-dir", "queue-once-state")
        once_job = json.loads(command("enqueue", "inbox/job.opscheck.json", *once_state))
        assert command("runs", *once_state).strip() == ""
        once_result = json.loads(command("worker", "--once", *once_state))
        assert once_result["status"] == "WAITING_FOR_APPROVAL"
        assert once_result["workflow_run_id"] == once_job["workflow_run_id"]
        assert "SUCCEEDED" in command("approve", once_job["workflow_run_id"], "--reviewer", "Package QA", *once_state)
        assert json.loads(command("job", once_job["id"], *once_state))["status"] == "SUCCEEDED"
        assert "Status: COMPLETED" in command("event", once_job["ingestion_id"], *once_state)
        assert "SUCCEEDED" in command("run", once_job["workflow_run_id"], *once_state)
        print("PASS: installed enqueue/no execution -> worker --once -> approval -> queue SUCCEEDED/event COMPLETED/workflow SUCCEEDED.")


if __name__ == "__main__":
    main()
