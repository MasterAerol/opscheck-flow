"""Deterministically clarify a briefing using verified evidence and human feedback."""
from __future__ import annotations

from copy import deepcopy

from .core import OpsCheckError


def revise_briefing(current: dict, verifier: dict, quality: dict, changes: dict,
                    feedback: str, revision_number: int) -> dict:
    """Add record-level explanations without changing verified facts or calling a model.

    Feedback selects the ordering of validation and change details. Both remain
    available; arbitrary requests are recorded, never executed as instructions.
    """
    if verifier.get("verified") is not True:
        raise OpsCheckError("Revision requires verified worker evidence.")
    result = deepcopy(current)
    focus = "changes" if any(word in feedback.lower() for word in ("change", "snapshot")) else "validation"
    sections = {
        "validation": {"title": "Validation issues by source record", "findings": quality["findings"],
                       "total": quality["summary"]["issues"]},
        "changes": {"title": "Snapshot changes by order ID", "findings": changes["findings"],
                    "total": changes["summary"]["total_changes"], "schema": changes["schema"]},
    }
    previous = current["briefing"]["summary"]
    # Preserve the initial summary instead of accumulating repeated revision prose.
    base = current.get("revision", {}).get("original_summary", previous)
    result["briefing"]["summary"] = (
        f"{base}\n\nRevision {revision_number}: see the {focus} details first. "
        "Validation details identify source rows, fields, values, and rule explanations; "
        "snapshot details identify order keys, changed fields, and before/after values."
    )
    result["revision"] = {
        "number": revision_number, "feedback": feedback, "mode": "local",
        "original_summary": base, "verified_checks": deepcopy(verifier["checks"]),
        "sections": [deepcopy(sections[name]) for name in (focus, "validation" if focus == "changes" else "changes")],
        "sampling_note": "Details use saved worker samples (up to 500 findings per source). Totals include omitted findings. Row numbers count logical CSV records, starting at 2.",
    }
    return result
