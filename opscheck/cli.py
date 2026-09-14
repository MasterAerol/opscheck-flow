"""Command line entry points and safe report output."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from importlib import resources

from . import __version__
from .core import OpsCheckError, compare, load_csv, load_rules, validate
from .report import render_html, render_text, render_workflow_html


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not 1 <= number <= 5000:
        raise argparse.ArgumentTypeError("must be between 1 and 5000")
    return number


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
    for command in (flow, flow_demo, runs):
        command.add_argument("--state-dir", type=Path, default=Path(".opscheck/runs"))
    for command in (flow, flow_demo):
        command.add_argument("--fail-once", choices=["quality_agent", "change_agent"],
                             help="demo only: inject a transient failure on the first attempt")
    return parser


def _same_file(left: Path, right: Path) -> bool:
    """Resolve relative paths and symlinks; samefile also catches hardlinks."""
    if left.resolve() == right.resolve():
        return True
    return left.exists() and right.exists() and left.samefile(right)


def _protect_inputs(outputs: list[Path], inputs: list[Path]) -> None:
    for index, output in enumerate(outputs):
        if output.exists() and not output.is_file():
            raise OpsCheckError(f"Report destination is not a file: {output}")
        for source in inputs:
            if _same_file(output, source):
                raise OpsCheckError(f"Report destination would overwrite an input: {output}")
        for other in outputs[:index]:
            if _same_file(output, other):
                raise OpsCheckError("Each report must have a different destination.")


def _atomic_write(path: Path, content: str) -> None:
    """Avoid leaving a half-written individual report if writing fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".opscheck-", delete=False) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


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
            _protect_inputs([extra_json], paths + [args.state_dir / "runs.sqlite3"])
        result = run_workflow(*paths, key=key, state_dir=args.state_dir, run_id=args.resume,
                              max_attempts=attempts, fail_once=args.fail_once, llm_url=llm_url,
                              model=model, max_rounds=rounds)
        destination = Path(result["state_dir"]) / result["run_id"]
        outputs = [destination / name for name in ("report.json", "report.html", "quality.html", "changes.html")]
        if extra_json is not None:
            outputs.append(extra_json)
        _protect_inputs(outputs, paths + [args.state_dir / "runs.sqlite3"])
        print(f"\nRun {result['run_id']}: {result['status'].upper()} ({result['mode']} mode)")
        print("Plan: planner -> [quality_agent + change_agent] -> verifier -> briefing_agent")
        for task in result["tasks"]:
            print(f"  {task['id']}: {task['status']} ({task['attempts']} attempt(s))")
            if task.get("error"):
                print(f"    {str(task['error'])!r}")
        data = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        _atomic_write(outputs[0], data)
        _atomic_write(outputs[1], render_workflow_html(result))
        for task_id, output in (("quality_agent", outputs[2]), ("change_agent", outputs[3])):
            if task_id in result["results"]:
                _atomic_write(output, render_html(result["results"][task_id]))
        if extra_json is not None:
            _atomic_write(extra_json, data)
        print(f"Report: {str(outputs[1])!r}")
        print("Workflow completion means execution succeeded; inspect the report for data issues.")
        return 0 if result["status"] == "succeeded" else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "demo":
            return _demo(args.out_dir)
        if args.command in ("flow", "flow-demo"):
            return _run_flow(args)
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
