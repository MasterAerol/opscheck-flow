"""CLI acceptance tests use real files and subprocesses."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "opscheck" / "examples"


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "opscheck", *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, encoding="utf-8", timeout=30)

    def check(self, filename, *extra):
        return self.command("validate", EXAMPLES / filename, "--rules", EXAMPLES / "rules.json", *extra)

    def test_clean_fixture_and_version(self):
        self.assertEqual(self.check("orders-valid.csv").returncode, 0)
        result = self.command("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("0.1.0", result.stdout)

    def test_issues_are_successfully_reported_with_exit_one(self):
        path = self.directory / "nested" / "issues.json"
        html = self.directory / "report.html"
        result = self.check("orders-messy.csv", "--json", path, "--html", html)
        self.assertEqual(result.returncode, 1, result.stderr)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["summary"]["issues"], 8)
        self.assertEqual(data["summary"]["affected_rows"], 8)
        self.assertIn("<!doctype html", html.read_text(encoding="utf-8").lower())

    def test_bad_input_is_exit_two_without_traceback(self):
        result = self.check("absent.csv")
        self.assertEqual(result.returncode, 2)
        self.assertIn("error", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_output_cannot_overwrite_input_or_alias(self):
        source = self.directory / "orders.csv"
        original = (EXAMPLES / "orders-valid.csv").read_bytes()
        source.write_bytes(original)
        result = self.command("validate", source, "--rules", EXAMPLES / "rules.json", "--json", source)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(source.read_bytes(), original)
        alias = self.directory / "alias.csv"
        os.link(source, alias)
        result = self.command("validate", source, "--rules", EXAMPLES / "rules.json", "--html", alias)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(source.read_bytes(), original)

    def test_json_and_html_cannot_share_a_destination(self):
        output = self.directory / "same"
        result = self.check("orders-valid.csv", "--json", output, "--html", output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())

    def test_capped_details_preserve_exact_issue_total(self):
        output = self.directory / "capped.json"
        result = self.check("orders-messy.csv", "--json", output, "--max-findings", 2)
        self.assertEqual(result.returncode, 1)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["issues"], 8)
        self.assertEqual(len(report["findings"]), 2)

    def test_compare_has_known_changes_and_distinct_exit_codes(self):
        output = self.directory / "changes.json"
        result = self.command("compare", EXAMPLES / "orders-before.csv", EXAMPLES / "orders-after.csv",
                              "--key", "order_id", "--json", output)
        self.assertEqual(result.returncode, 1, result.stderr)
        summary = json.loads(output.read_text(encoding="utf-8"))["summary"]
        self.assertEqual((summary["added"], summary["removed"], summary["changed"], summary["unchanged"]),
                         (1, 1, 2, 7))
        same = self.command("compare", EXAMPLES / "orders-before.csv", EXAMPLES / "orders-before.csv",
                            "--key", "order_id")
        self.assertEqual(same.returncode, 0, same.stderr)

    def test_demo_runs_offline_and_produces_four_reports(self):
        result = self.command("demo", "--out-dir", self.directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({file.name for file in self.directory.iterdir()},
                         {"validation.json", "validation.html", "comparison.json", "comparison.html"})

    def test_flow_demo_retry_resume_and_history(self):
        state = self.directory / "state"
        first = self.command("flow-demo", "--state-dir", state, "--fail-once", "quality_agent")
        self.assertEqual(first.returncode, 0, first.stderr)
        reports = list(state.glob("*/report.json"))
        self.assertEqual(len(reports), 1)
        run = json.loads(reports[0].read_text(encoding="utf-8"))
        quality = next(task for task in run["tasks"] if task["id"] == "quality_agent")
        self.assertEqual(quality["attempts"], 2)
        self.assertTrue((reports[0].parent / "report.html").exists())
        resumed = self.command("flow-demo", "--state-dir", state, "--resume", run["run_id"])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        second = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual([task["attempts"] for task in run["tasks"]],
                         [task["attempts"] for task in second["tasks"]])
        history = self.command("runs", "--state-dir", state)
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertIn(run["run_id"], history.stdout)

    def test_invalid_limits_and_partial_model_configuration(self):
        result = self.check("orders-valid.csv", "--max-findings", "0")
        self.assertEqual(result.returncode, 2)
        result = self.command("flow", EXAMPLES / "orders-messy.csv", "--rules", EXAMPLES / "rules.json",
                              "--before", EXAMPLES / "orders-before.csv", "--after", EXAMPLES / "orders-after.csv",
                              "--state-dir", self.directory, "--model", "missing-endpoint")
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_quoted_tilde_input_is_protected_and_state_reports_use_expanded_directory(self):
        with tempfile.TemporaryDirectory(prefix="opscheck-test-", dir=Path.home()) as folder:
            root = Path(folder)
            source = root / "input.csv"
            original = (EXAMPLES / "orders-messy.csv").read_bytes()
            source.write_bytes(original)
            quoted_source = Path("~") / root.name / "input.csv"
            result = self.command("flow", quoted_source, "--rules", EXAMPLES / "rules.json",
                                  "--before", EXAMPLES / "orders-before.csv", "--after", EXAMPLES / "orders-after.csv",
                                  "--state-dir", self.directory / "state", "--json", source)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(source.read_bytes(), original)
            quoted_state = Path("~") / root.name / "state"
            result = self.command("flow-demo", "--state-dir", quoted_state)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(list((root / "state").glob("*/report.html"))), 1)


if __name__ == "__main__":
    unittest.main()
