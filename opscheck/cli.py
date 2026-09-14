"""Command line entry points and safe report output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from importlib import resources

from .artifacts import _atomic_write, _protect_inputs
from . import __version__
from .core import OpsCheckError, compare, load_csv, load_rules, validate
from .report import render_html, render_text


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= number <= 5000:
        raise argparse.ArgumentTypeError("must be between 1 and 5000")
    return number


def _comment(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("rejection requires a nonblank comment")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="opscheck",
        description="Check CSV exports and compare snapshots. Files stay on your computer.",
    )
    parser.add_argument("--version", action="version", version=f"OpsCheck {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("validate", help="check a CSV against JSON rules")
    check.add_argument("input", type=Path)
    check.add_argument("--rules", required=True, type=Path)
    diff = commands.add_parser("compare", help="find added, removed, and changed records")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    diff.add_argument("--key", required=True, help="unique, nonblank ID column")
    for command in (check, diff):
        command.add_argument("--json", type=Path, metavar="PATH", help="save a JSON report")
        command.add_argument("--html", type=Path, metavar="PATH", help="save an HTML report")
        command.add_argument("--delimiter", default=",", help="one-character CSV delimiter (default: comma)")
        command.add_argument("--max-findings", type=_limit, default=500,
                             help="maximum detailed findings, 1–5000 (default: 500); totals remain exact")
    demo = commands.add_parser("demo", help="run both workflows with bundled synthetic orders")
    demo.add_argument("--out-dir", type=Path, default=Path("reports/demo"))
    flow = commands.add_parser("flow", help="run or resume the persistent agent workflow")
    flow.add_argument("input", type=Path)
    flow.add_argument("--rules", required=True, type=Path)
    flow.add_argument("--before", required=True, type=Path)
    flow.add_argument("--after", required=True, type=Path)
    flow.add_argument("--key", default="order_id")
    flow.add_argument("--resume", metavar="RUN_ID")
    flow.add_argument("--max-attempts", type=int, default=2)
    flow.add_argument("--llm-url", help="optional local chat/completions endpoint")
    flow.add_argument("--model", help="model already available on your local server")
    flow.add_argument("--max-rounds", type=int, default=2)
    flow.add_argument("--json", type=Path, metavar="PATH")
    flow_demo = commands.add_parser("flow-demo", help="demonstrate parallel workers, retry, and saved state")
    flow_demo.add_argument("--resume", metavar="RUN_ID")
    runs = commands.add_parser("runs", help="list saved workflow runs")
    event_commands = []
    for name, help_text in (("ingest", "ingest a JSON job manifest"), ("scan", "scan an inbox once for *.opscheck.json"),
                            ("events", "list durable ingestion events"), ("event", "inspect an ingestion event and audit history"),
                            ("retry-event", "retry or recover a reserved event workflow")):
        command = commands.add_parser(name, help=help_text)
        if name == "ingest":
            command.add_argument("manifest", type=Path)
        elif name == "scan":
            command.add_argument("inbox", type=Path)
        elif name in ("event", "retry-event"):
            command.add_argument("event_id")
        event_commands.append(command)
    approval_commands = []
    for name in ("approve", "reject", "approvals", "run", "resume"):
        command = commands.add_parser(name, help={
            "approve": "approve the pending briefing and finish",
            "reject": "reject the pending briefing and revise",
            "approvals": "show persisted approval history",
            "run": "inspect a saved run", "resume": "resume using saved configuration",
        }[name])
        command.add_argument("run_id")
        if name in ("approve", "reject"):
            command.add_argument("--reviewer")
            command.add_argument("--comment", required=name == "reject", type=_comment if name == "reject" else str)
            command.add_argument("--approval-id", help="decide only this exact approval version")
        if name == "resume":
            command.add_argument("--max-attempts", type=int, default=2)
        approval_commands.append(command)
    for command in (flow, flow_demo, runs, *approval_commands, *event_commands):
        command.add_argument("--state-dir", type=Path, default=Path(".opscheck/runs"))
    for command in (flow, flow_demo):
        command.add_argument("--max-human-revisions", type=int, default=3,
                             help="fail after this many rejected versions (1-20; default: 3)")
        command.add_argument("--fail-once", choices=["quality_agent", "change_agent"],
                             help="demo only: inject a transient failure on the first attempt")
    return parser


def _save(result: dict, json_path: Path | None, html_path: Path | None) -> None:
    # Render both before writing so a rendering error cannot produce one stale report.
    prepared = []
    if json_path is not None:
        prepared.append((json_path, json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"))
    if html_path is not None:
        prepared.append((html_path, render_html(result)))
    for path, content in prepared:
        _atomic_write(path, content)
        print(f"Saved {str(path)!r}")


def _demo(out_dir: Path) -> int:
    examples = resources.files("opscheck").joinpath("examples")
    names = ["orders-messy.csv", "rules.json", "orders-before.csv", "orders-after.csv"]
    # Installed wheels are unpacked by pip; as_file also supports other resource loaders.
    from contextlib import ExitStack
    with ExitStack() as stack:
        paths = [stack.enter_context(resources.as_file(examples.joinpath(name))) for name in names]
        outputs = [out_dir / f"{kind}.{extension}"
                   for kind in ("validation", "comparison") for extension in ("json", "html")]
        _protect_inputs(outputs, paths)
        validation = validate(load_csv(paths[0]), load_rules(paths[1]))
        comparison = compare(load_csv(paths[2]), load_csv(paths[3]), "order_id")
        print("Synthetic demo data — intentional validation issues and snapshot changes.\n")
        for result, index in ((validation, 0), (comparison, 2)):
            print(render_text(result))
            _save(result, outputs[index], outputs[index + 1])
    print("\nDemo complete. Open validation.html and comparison.html in your browser.")
    return 0


def _run_flow(args: argparse.Namespace) -> int:
    from contextlib import ExitStack
    from .workflow import run_workflow

    # Use identical paths for collision checks, engine reads, state, and reports.
    # In particular, the workflow engine expands a quoted '~' path.
    args.state_dir = args.state_dir.expanduser().absolute()
    with ExitStack() as stack:
        if args.command == "flow-demo":
            examples = resources.files("opscheck").joinpath("examples")
            paths = [stack.enter_context(resources.as_file(examples.joinpath(name))) for name in
                     ("orders-messy.csv", "rules.json", "orders-before.csv", "orders-after.csv")]
            key, attempts, llm_url, model, rounds = "order_id", 2, None, None, 2
            extra_json = None
            print("Local demo: synthetic data, specialized tool workers, no model or API required.")
        else:
            paths = [path.expanduser().absolute() for path in
                     (args.input, args.rules, args.before, args.after)]
            key, attempts, llm_url, model, rounds = (args.key, args.max_attempts, args.llm_url,
                                                   args.model, args.max_rounds)
            extra_json = args.json.expanduser().absolute() if args.json is not None else None
        if extra_json is not None:
            database = args.state_dir / "runs.sqlite3"
            protected = paths + [Path(str(database) + suffix) for suffix in ("", "-journal", "-wal", "-shm")]
            protected += list(args.state_dir.glob("*.lock"))
            _protect_inputs([extra_json], protected)
        result = run_workflow(*paths, key=key, state_dir=args.state_dir, run_id=args.resume,
                              max_attempts=attempts, fail_once=args.fail_once, llm_url=llm_url,
                              model=model, max_rounds=rounds, max_human_revisions=args.max_human_revisions)
        destination = Path(result["state_dir"]) / result["run_id"]
        outputs = [destination / name for name in ("report.json", "report.html", "quality.html", "changes.html")]
        outputs += [destination / f"briefing-v{item['iteration']}.json" for item in result["approvals"]]
        if extra_json is not None:
            outputs.append(extra_json)
        _protect_inputs(outputs, paths + [args.state_dir / "runs.sqlite3"])
        print(f"\nRun {result['run_id']}: {result['status'].upper()} ({result['mode']} mode)")
        print("Plan: planner -> [quality_agent + change_agent] -> verifier -> briefing_agent -> approval_gate")
        for task in result["tasks"]:
            print(f"  {task['id']}: {task['status']} ({task['attempts']} attempt(s))")
            if task.get("error"):
                print(f"    {str(task['error'])!r}")
        data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if extra_json is not None:
            _atomic_write(extra_json, data)
        _print_run(result)
        return 2 if result["status"] == "FAILED" else 0


def _print_run(result: dict) -> None:
    print(f"Run: {result['run_id']}\nStatus: {result['status']}")
    print(f"Approval iteration: {result['approval_iteration']}")
    print(f"Rejected revisions: {result['rejected_revisions']}")
    print(f"Pending approval: {result['active_approval_id'] or 'None'}")
    print(f"Report: {result['report_path']!r}")
    if result.get("failure_reason"):
        print(f"Failure reason: {result['failure_reason']!r}")
    if result["status"] == "WAITING_FOR_APPROVAL":
        print("Workflow paused. Run is waiting for human approval.")
        directory = result['state_dir'].replace("'", "''")
        suffix = f"--state-dir '{directory}' --approval-id {result['active_approval_id']}"
        print(f"Approve: py -3.12 -m opscheck approve {result['run_id']} --reviewer 'Aerol' {suffix}")
        print(f"Reject: py -3.12 -m opscheck reject {result['run_id']} --reviewer 'Aerol' --comment 'Explain affected records.' {suffix}")


def _human_command(args: argparse.Namespace) -> int:
    from .approvals import decide, inspect_run, resume_run
    if args.command in ("approve", "reject"):
        decision = "approved" if args.command == "approve" else "rejected"
        result = decide(args.run_id, decision, args.reviewer, args.comment, args.state_dir, args.approval_id)
        print(f"Approval: {decision.upper()}\nWorkflow resumed.")
        if args.command == "reject" and result["status"] == "WAITING_FOR_APPROVAL":
            print("revision_agent completed using verified evidence and human feedback.")
    elif args.command == "resume":
        result = resume_run(args.run_id, args.state_dir, args.max_attempts)
    else:
        result = inspect_run(args.run_id, args.state_dir)
    if args.command == "approvals":
        print(f"Approval history for run {args.run_id}")
        for item in result["approvals"]:
            print(f"\n#{item['iteration']} {item['status'].upper()}\nApproval ID: {item['id']}")
            print(f"Reviewer: {item['reviewer']!r}\nComment: {item['comment']!r}")
            print(f"Created: {item['created_at']}\nDecided: {item['decided_at'] or 'Pending'}")
            print(f"Briefing: {item['briefing_reference']!r}")
        return 0
    _print_run(result)
    return 2 if args.command in ("reject", "resume") and result["status"] == "FAILED" else 0


def _print_event(event: dict, history: bool = False) -> None:
    if event.get("duplicate"):
        print("Duplicate event detected. Reusing the canonical event and reserved run.")
    print(f"Event: {event['id']}\nStatus: {event['status']}")
    print(f"External event ID: {event['event_id']!r}\nEvent type: {event['event_type']!r}")
    print(f"Fingerprint: {event['fingerprint'] or '-'}\nManifest: {event['manifest_path']!r}")
    print(f"Run: {event['workflow_run_id'] or '-'}\nWorkflow status: {event['workflow_status'] or 'Not created'}")
    print(f"Duplicate count: {event['duplicate_count']}\nRetryable: {event['retryable']}")
    for key in ("received_at", "updated_at", "claimed_at", "completed_at"):
        print(f"{key}: {event[key] or '-'}")
    if event['error']:
        print(f"Error: {event['error']!r}")
    if event.get('processing_error') and event['processing_error'] != event['error']:
        print(f"Processing error saved in audit history: {event['processing_error']!r}")
        print("The workflow checkpoint is saved. Fix the filesystem issue and use resume RUN_ID to rebuild reports.")
    if history:
        print("External IDs: " + json.dumps(event["external_event_ids"], ensure_ascii=True))
        print("Audit history:")
        for entry in event["history"]:
            print(json.dumps(entry, ensure_ascii=True, sort_keys=True))


def _event_command(args: argparse.Namespace) -> int:
    from .ingestion import ingest, inspect_event, list_events, retry_event, scan
    if args.command == "events":
        for event in list_events(args.state_dir):
            print(json.dumps({key: event[key] for key in
                  ("id", "event_id", "status", "workflow_run_id", "duplicate_count", "error")}, ensure_ascii=True))
        return 0
    if args.command == "scan":
        result = scan(args.inbox, args.state_dir)
        for event in result["events"]:
            print(json.dumps({key: event.get(key) for key in ("id", "manifest_path", "status", "workflow_run_id", "error", "processing_error")}, ensure_ascii=True))
        for key in ("scanned", "new_events", "duplicates", "invalid", "failed", "workflows_created"):
            print(f"{key.replace('_', ' ').capitalize()}: {result[key]}")
        return 2 if result["invalid"] or result["failed"] else 0
    if args.command == "ingest":
        event = ingest(args.manifest, args.state_dir)
    elif args.command == "retry-event":
        event = retry_event(args.event_id, args.state_dir)
    else:
        event = inspect_event(args.event_id, args.state_dir)
    _print_event(event, history=args.command == "event")
    return 2 if args.command != "event" and (event["status"] in ("INVALID", "FAILED") or event.get("processing_error")) else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in ("ingest", "scan", "events", "event", "retry-event"):
            return _event_command(args)
        if args.command == "demo":
            return _demo(args.out_dir)
        if args.command in ("flow", "flow-demo"):
            return _run_flow(args)
        if args.command in ("approve", "reject", "approvals", "run", "resume"):
            return _human_command(args)
        if args.command == "runs":
            from .workflow import list_runs
            for run in list_runs(args.state_dir):
                print(json.dumps(run, ensure_ascii=True, sort_keys=True))
            return 0
        if len(args.delimiter) != 1 or args.delimiter in ('\r', '\n', '"', '\0'):
            raise OpsCheckError("Delimiter must be one character other than a quote, newline, or NUL.")
        inputs = [args.input, args.rules] if args.command == "validate" else [args.before, args.after]
        _protect_inputs([path for path in (args.json, args.html) if path is not None], inputs)
        if args.command == "validate":
            result = validate(load_csv(args.input, args.delimiter), load_rules(args.rules), args.max_findings)
        else:
            result = compare(load_csv(args.before, args.delimiter), load_csv(args.after, args.delimiter),
                             args.key, args.max_findings)
        print(render_text(result))
        _save(result, args.json, args.html)
        return 0 if result["status"] in ("pass", "unchanged") else 1
    except (OpsCheckError, OSError, UnicodeError, ValueError, RecursionError) as exc:
        # repr escapes control characters in untrusted filenames and error details.
        print(f"opscheck: error: {str(exc)!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
