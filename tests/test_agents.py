"""Offline adapter tests against an actual local HTTP server, without a model."""
from __future__ import annotations

import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from opscheck.agents import (
    AgentError,
    MAX_EVIDENCE_BYTES,
    MAX_RESPONSE_BYTES,
    TransportError,
    build_evidence,
    run_agent_team,
)


def sample_evidence():
    return {
        "quality_summary": {"rows": 2, "issues": 1, "affected_rows": 1, "reported_findings": 1},
        "change_summary": {"total_changes": 0},
        "evidence": [{"id": "V1", "row": 2, "column": "email", "code": "email", "value": "bad"}],
        "truncation": {"applied": False},
    }


def briefing(title="Check the email in row 2", ids=None):
    return {
        "summary": "One email needs inspection.",
        "actions": [{"title": title, "priority": "medium", "evidence_ids": ["V1"] if ids is None else ids}],
    }


def chat_reply(value):
    return json.dumps({"choices": [{"message": {"content": json.dumps(value)}}]}).encode()


@contextmanager
def fake_server(replies):
    """Replies can be model JSON values, raw bytes, or (status, headers, bytes)."""
    requests = []
    pending = list(replies)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            requests.append({"path": self.path, "body": json.loads(self.rfile.read(length))})
            if not pending:
                status, headers, body = 500, {}, b"unexpected extra request"
            else:
                reply = pending.pop(0)
                if isinstance(reply, tuple):
                    status, headers, body = reply
                else:
                    status, headers = 200, {}
                    body = reply if isinstance(reply, bytes) else chat_reply(reply)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/chat/completions", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class AgentTeamTests(unittest.TestCase):
    def test_rejected_briefing_is_revised_and_approved_in_separate_contexts(self):
        with fake_server([
            briefing("Fix all emails"),
            {"approved": False, "feedback": "Only row 2 is supported. Name row 2."},
            briefing(),
            {"approved": True, "feedback": "Actions match the evidence."},
        ]) as (url, requests):
            result = run_agent_team(sample_evidence(), url, "test-local-model", max_rounds=2)
        self.assertEqual(result["rounds"], 2)
        self.assertTrue(result["approved"])
        self.assertEqual([entry["approved"] for entry in result["review_history"]], [False, True])
        self.assertEqual(result["briefing"]["actions"][0]["title"], "Check the email in row 2")
        self.assertEqual(len(requests), 4)
        analyst, reviewer, revised, _ = [item["body"] for item in requests]
        self.assertIn("operations analyst", analyst["messages"][0]["content"])
        self.assertIn("independent quality reviewer", reviewer["messages"][0]["content"])
        self.assertNotEqual(analyst["messages"][0], reviewer["messages"][0])
        self.assertEqual(len(reviewer["messages"]), 2)
        revision = json.loads(revised["messages"][1]["content"])
        self.assertIn("Only row 2", revision["correction_feedback"])
        self.assertIn("previous_briefing", revision)
        for request in requests:
            body = request["body"]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["model"], "test-local-model")
            self.assertIs(body["stream"], False)
            self.assertEqual(body["temperature"], 0)
            self.assertNotIn("tools", body)

    def test_nonexistent_evidence_is_corrected_without_reviewing_invalid_draft(self):
        with fake_server([
            briefing(ids=["V999"]), briefing(), {"approved": True, "feedback": "Supported."},
        ]) as (url, requests):
            result = run_agent_team(sample_evidence(), url, "test-model", 2)
        self.assertEqual(len(requests), 3)
        self.assertEqual(result["rounds"], 2)
        self.assertIn("schema validation failed", result["review_history"][0]["feedback"])
        revision = json.loads(requests[1]["body"]["messages"][1]["content"])
        self.assertIn("supplied evidence", revision["correction_feedback"])

    def test_exhausted_reviews_raise_nonretryable_error_with_actual_history(self):
        rejection = {"approved": False, "feedback": "Summary is not supported."}
        with fake_server([briefing(), rejection, briefing(), rejection]) as (url, requests):
            with self.assertRaisesRegex(AgentError, "exhausted 2 rounds") as caught:
                run_agent_team(sample_evidence(), url, "test-model", 2)
        self.assertEqual(len(requests), 4)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual([item["round"] for item in caught.exception.review_history], [1, 2])

    def test_reviewer_boolean_must_be_boolean_and_cannot_autoapprove(self):
        with fake_server([briefing(), {"approved": "true", "feedback": "Fine."}]) as (url, requests):
            with self.assertRaisesRegex(AgentError, "without approval") as caught:
                run_agent_team(sample_evidence(), url, "test-model", 1)
        self.assertEqual(len(requests), 2)
        self.assertIn("Reviewer schema validation failed", caught.exception.review_history[0]["feedback"])

    def test_invalid_analyst_shapes_do_not_reach_reviewer(self):
        invalid = [
            {"summary": "", "actions": []},
            {"summary": "Something", "actions": [], "extra": True},
            {"summary": "Something", "actions": "none"},
            briefing(ids=[]),
            briefing(ids=["V1", "V1"]),
            briefing(ids=[False]),
            briefing(title=" "),
            {"summary": "Something", "actions": [{"title": "Check", "priority": "urgent", "evidence_ids": ["V1"]}]},
        ]
        for value in invalid:
            with self.subTest(value=value), fake_server([value]) as (url, requests):
                with self.assertRaises(AgentError):
                    run_agent_team(sample_evidence(), url, "test-model", 1)
                self.assertEqual(len(requests), 1)

    def test_duplicate_json_keys_are_rejected(self):
        response = json.dumps({"choices": [{"message": {"content": '{"summary":"first","summary":"second","actions":[]}'}}]}).encode()
        with fake_server([response]) as (url, requests):
            with self.assertRaisesRegex(AgentError, "duplicate keys"):
                run_agent_team(sample_evidence(), url, "test-model", 1)
        self.assertEqual(len(requests), 1)

    def test_empty_actions_can_be_approved_for_clean_evidence(self):
        evidence = {"quality_summary": {"issues": 0}, "change_summary": {"total_changes": 0}, "evidence": []}
        with fake_server([
            {"summary": "The supplied checks found no issues or changes.", "actions": []},
            {"approved": True, "feedback": ""},
        ]) as (url, _):
            result = run_agent_team(evidence, url, "test-model", 1)
        self.assertEqual(result["briefing"]["actions"], [])

    def test_remote_or_ambiguous_urls_are_rejected_before_requests(self):
        urls = [
            "https://example.com/v1/chat/completions",
            "http://127.0.0.2/v1/chat/completions",
            "http://127.1/v1/chat/completions",
            "http://2130706433/v1/chat/completions",
            "http://user:password@127.0.0.1/v1/chat/completions",
            "http://127.0.0.1/v1/chat/completions?api_key=secret",
            "http://127.0.0.1/v1/chat/completions?",
            "http://127.0.0.1/v1/chat/completions#fragment",
            "http://localhost.example.com/v1/chat/completions",
            "http://127.0.0.1:70000/v1/chat/completions",
            "file:///v1/chat/completions",
            "http://127.0.0.1/api/chat",
            "http://[::ffff:127.0.0.1]/v1/chat/completions",
            " http://127.0.0.1/v1/chat/completions",
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(AgentError):
                run_agent_team(sample_evidence(), url, "test-model")

    def test_redirects_are_not_followed_even_to_same_local_server(self):
        with fake_server([(302, {"Location": "/elsewhere"}, b""), briefing()]) as (url, requests):
            with self.assertRaisesRegex(AgentError, "redirects are disabled") as caught:
                run_agent_team(sample_evidence(), url, "test-model")
        self.assertEqual(len(requests), 1)
        self.assertFalse(caught.exception.retryable)

    def test_localhost_works_with_invalid_environment_proxies(self):
        with fake_server([briefing(), {"approved": True, "feedback": "Supported."}]) as (url, requests):
            with patch.dict("os.environ", {
                "HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1", "all_proxy": "http://127.0.0.1:1",
                "NO_PROXY": "", "no_proxy": "",
            }):
                result = run_agent_team(sample_evidence(), url.replace("127.0.0.1", "localhost"), "test-model")
        self.assertTrue(result["approved"])
        self.assertEqual(len(requests), 2)

    def test_response_limit_is_enforced_on_actual_http_body(self):
        with fake_server([b"x" * (MAX_RESPONSE_BYTES + 1)]) as (url, requests):
            with self.assertRaisesRegex(AgentError, "response exceeds"):
                run_agent_team(sample_evidence(), url, "test-model")
        self.assertEqual(len(requests), 1)

    def test_evidence_limit_is_enforced_before_request(self):
        evidence = sample_evidence()
        evidence["extra"] = "x" * MAX_EVIDENCE_BYTES
        with fake_server([]) as (url, requests):
            with self.assertRaisesRegex(AgentError, "Evidence exceeds"):
                run_agent_team(evidence, url, "test-model")
        self.assertEqual(requests, [])

    def test_transient_http_failure_is_retryable_for_workflow(self):
        for code in (408, 429, 500, 503):
            with self.subTest(code=code), fake_server([(code, {}, b"error")]) as (url, requests):
                with self.assertRaises(TransportError) as caught:
                    run_agent_team(sample_evidence(), url, "test-model")
                self.assertTrue(caught.exception.retryable)
                self.assertEqual(len(requests), 1)

    def test_invalid_envelope_is_nonretryable(self):
        for response in (b"not json", b'{"choices": []}', b'{"choices":[{"message":{"content":null}}]}'):
            with self.subTest(response=response), fake_server([response]) as (url, _):
                with self.assertRaisesRegex(AgentError, "response envelope") as caught:
                    run_agent_team(sample_evidence(), url, "test-model")
                self.assertFalse(caught.exception.retryable)

    def test_round_limits_and_model_are_validated_without_request(self):
        with fake_server([]) as (url, requests):
            for rounds in (0, 4, True, "2", 1.5):
                with self.subTest(rounds=rounds), self.assertRaisesRegex(AgentError, "max_rounds"):
                    run_agent_team(sample_evidence(), url, "test-model", rounds)
            for model in ("", " ", None, "x" * 201):
                with self.subTest(model=model), self.assertRaisesRegex(AgentError, "model"):
                    run_agent_team(sample_evidence(), url, model)
        self.assertEqual(requests, [])


class EvidenceTests(unittest.TestCase):
    def test_ids_are_deterministic_and_schema_changes_have_evidence(self):
        quality = {"summary": {"issues": 1}, "findings": [{"row": 2, "column": "email", "value": "bad"}]}
        changes = {
            "summary": {"total_changes": 1},
            "schema": {"added": ["status"], "removed": ["legacy"]},
            "findings": [{"kind": "changed", "key": "1", "before": {"email": "a"}, "after": {"email": "b"}, "fields": ["email"]}],
        }
        evidence = build_evidence(quality, changes)
        self.assertEqual(evidence, build_evidence(quality, changes))
        self.assertEqual([item["id"] for item in evidence["evidence"]], ["V1", "C1", "C2"])
        self.assertEqual(evidence["evidence"][1]["kind"], "schema")
        self.assertEqual(evidence["evidence"][1]["added"], ["status"])
        self.assertFalse(evidence["truncation"]["applied"])

    def test_sampling_and_value_truncation_are_disclosed_without_mutation(self):
        quality = {
            "summary": {"issues": 30, "rows": 30},
            "findings": [{"row": index + 2, "column": "note", "value": "x" * 1000} for index in range(30)],
        }
        changes = {"summary": {"total_changes": 0}, "schema": {"added": [], "removed": []}, "findings": []}
        evidence = build_evidence(quality, changes)
        self.assertEqual(len(evidence["evidence"]), 20)
        self.assertTrue(evidence["truncation"]["applied"])
        self.assertEqual(evidence["truncation"]["quality_available_findings"], 30)
        self.assertEqual(evidence["truncation"]["quality_included_findings"], 20)
        self.assertEqual(evidence["quality_summary"]["issues"], 30)
        self.assertIn("[truncated]", evidence["evidence"][0]["value"])
        self.assertEqual(len(quality["findings"][0]["value"]), 1000)

    def test_engine_omitted_findings_are_disclosed(self):
        evidence = build_evidence(
            {"summary": {"issues": 10}, "findings": []},
            {"summary": {"total_changes": 0}, "findings": [], "schema": {}},
        )
        self.assertTrue(evidence["truncation"]["applied"])
        self.assertEqual(evidence["quality_summary"]["issues"], 10)

    def test_global_evidence_byte_cap_with_wide_unicode_records(self):
        changes = {
            "summary": {"total_changes": 20},
            "schema": {},
            "findings": [{
                "kind": "changed", "key": str(index),
                "before": {f"field_{field}": "界" * 500 for field in range(20)},
                "after": {f"field_{field}": "面" * 500 for field in range(20)},
                "fields": [f"field_{field}" for field in range(20)],
            } for index in range(20)],
        }
        evidence = build_evidence({"summary": {"issues": 0}, "findings": []}, changes)
        self.assertLessEqual(len(json.dumps(evidence, ensure_ascii=False, separators=(",", ":")).encode()), MAX_EVIDENCE_BYTES)
        self.assertTrue(evidence["truncation"]["applied"])
        self.assertLess(evidence["truncation"]["change_included_findings"], 20)
        self.assertGreater(evidence["truncation"]["change_included_findings"], 0)


if __name__ == "__main__":
    unittest.main()
