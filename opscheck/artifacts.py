"""Safe, replaceable report artifacts derived from durable workflow state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from .core import OpsCheckError
from .report import render_html, render_workflow_html


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


def write_workflow_reports(result: dict, sources: list[Path]) -> None:
    """Write a consistent snapshot while the caller owns the run execution lock."""
    destination = Path(result["state_dir"]) / result["run_id"]
    prepared = {
        destination / "report.json": json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        destination / "report.html": render_workflow_html(result),
    }
    for task, name in (("quality_agent", "quality.html"), ("change_agent", "changes.html")):
        if task in result["results"]:
            prepared[destination / name] = render_html(result["results"][task])
    for approval in result["approvals"]:
        prepared[destination / f"briefing-v{approval['iteration']}.json"] = (
            json.dumps(approval["briefing"], ensure_ascii=False, indent=2) + "\n")
    database = Path(result["state_dir"]) / "runs.sqlite3"
    protected = sources + [Path(str(database) + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    protected += list(Path(result["state_dir"]).glob("*.lock"))
    _protect_inputs(list(prepared), protected)
    for path, content in prepared.items():
        _atomic_write(path, content)
