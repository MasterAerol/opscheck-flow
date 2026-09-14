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
        def command(*args: str) -> str:
            completed = subprocess.run([sys.executable, "-m", "opscheck", *args], cwd=directory,
                                       env=environment, capture_output=True, encoding="utf-8", timeout=30)
            if completed.returncode != 0:
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


if __name__ == "__main__":
    main()
