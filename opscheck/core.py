"""Deterministic CSV validation and comparison, without third-party dependencies."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
import re
from threading import RLock
from typing import Any


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_RECORDS = 100_000
_CSV_LOCK = RLock()
_TYPES = {"string", "integer", "number", "email", "date"}
_RULE_KEYS = {"required", "unique", "type", "min", "max", "allowed"}
_INTEGER = re.compile(r"[+-]?[0-9]+\Z")
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


class OpsCheckError(ValueError):
    """An input or configuration problem that prevents reliable processing."""


@dataclass(frozen=True)
class Dataset:
    """Loaded CSV values; headers and cell text preserve their original spelling."""

    source: str
    headers: list[str]
    rows: list[dict[str, str]]


def load_csv(path: str | Path, delimiter: str = ",") -> Dataset:
    """Read a bounded UTF-8 CSV, rejecting ambiguous structure."""
    if not isinstance(delimiter, str) or len(delimiter) != 1 or delimiter in '\r\n\0"':
        raise OpsCheckError("Delimiter must be one character other than a newline, NUL, or double quote.")
    source = Path(path).name
    try:
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise OpsCheckError(f"Cannot read CSV {source}: {exc.strerror or 'I/O error'}.") from exc
    if len(data) > MAX_FILE_BYTES:
        raise OpsCheckError(f"CSV {source} exceeds the 10 MiB size limit.")
    try:
        contents = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OpsCheckError(f"CSV {source} must use UTF-8 encoding.") from exc
    # csv.field_size_limit is process-global. Keep its temporary change and all
    # parsing in one critical section when workflow workers load concurrently.
    with _CSV_LOCK:
        return _parse_csv(contents, source, delimiter)


def _parse_csv(contents: str, source: str, delimiter: str) -> Dataset:
    previous_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(MAX_FILE_BYTES)
        reader = csv.reader(io.StringIO(contents, newline=""), delimiter=delimiter, strict=True)
        headers = next(reader, None)
        if not headers:
            raise OpsCheckError(f"CSV {source} has no header record.")
        if any(not header.strip() for header in headers):
            raise OpsCheckError(f"CSV {source} contains a blank column header.")
        if len(set(headers)) != len(headers):
            raise OpsCheckError(f"CSV {source} contains duplicate column headers.")
        rows: list[dict[str, str]] = []
        for record, values in enumerate(reader, start=2):
            if len(rows) >= MAX_RECORDS:
                raise OpsCheckError(f"CSV {source} exceeds the 100,000 record limit.")
            if len(values) != len(headers):
                raise OpsCheckError(
                    f"CSV {source}, record {record}: expected {len(headers)} fields, "
                    f"received {len(values)}."
                )
            rows.append(dict(zip(headers, values)))
    except csv.Error as exc:
        raise OpsCheckError(f"Malformed CSV {source}: {exc}.") from exc
    finally:
        csv.field_size_limit(previous_limit)
    return Dataset(source=source, headers=headers, rows=rows)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OpsCheckError(f"Rules JSON contains duplicate key {key!r}.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise OpsCheckError(f"Rules JSON contains non-finite number {value}.")


def _bound(value: Any, context: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise OpsCheckError(f"{context} must be a finite numeric value.")
    try:
        number = Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise OpsCheckError(f"{context} must be a finite numeric value.") from exc
    if not number.is_finite():
        raise OpsCheckError(f"{context} must be a finite numeric value.")
    return number


def _check_rules(rules: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(rules, dict):
        raise OpsCheckError("Rules must be a JSON object.")
    if set(rules) != {"version", "columns"}:
        raise OpsCheckError("Rules must contain only the required keys 'version' and 'columns'.")
    if type(rules["version"]) is not int or rules["version"] != 1:
        raise OpsCheckError("Rules version must be the integer 1.")
    columns = rules["columns"]
    if not isinstance(columns, dict):
        raise OpsCheckError("Rules 'columns' must be an object mapping column names to rules.")
    for column, rule in columns.items():
        if not isinstance(column, str) or not column.strip():
            raise OpsCheckError("Rule column names must be nonblank strings.")
        if not isinstance(rule, dict):
            raise OpsCheckError(f"Rules for {column!r} must be an object.")
        if not all(isinstance(key, str) for key in rule) or set(rule) - _RULE_KEYS:
            raise OpsCheckError(f"Rules for {column!r} contain an unknown rule key.")
        for flag in ("required", "unique"):
            if flag in rule and not isinstance(rule[flag], bool):
                raise OpsCheckError(f"{column!r}.{flag} must be a boolean.")
        column_type = rule.get("type", "string")
        if not isinstance(column_type, str) or column_type not in _TYPES:
            raise OpsCheckError(f"{column!r}.type must be one of {', '.join(sorted(_TYPES))}.")
        for name in ("min", "max"):
            if name in rule:
                if column_type not in {"number", "integer"}:
                    raise OpsCheckError(f"{column!r}.{name} requires type 'number' or 'integer'.")
                _bound(rule[name], f"{column!r}.{name}")
        if "min" in rule and "max" in rule:
            if _bound(rule["min"], "min") > _bound(rule["max"], "max"):
                raise OpsCheckError(f"{column!r}.min must not exceed max.")
        if "allowed" in rule:
            allowed = rule["allowed"]
            if not isinstance(allowed, list) or not allowed or not all(isinstance(v, str) for v in allowed):
                raise OpsCheckError(f"{column!r}.allowed must be a nonempty list of strings.")
    return columns


def load_rules(path: str | Path) -> dict[str, Any]:
    """Load strict JSON rules and reject duplicate keys and non-finite numbers."""
    source = Path(path).name
    try:
        contents = Path(path).read_text(encoding="utf-8-sig")
        rules = json.loads(
            contents,
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except OSError as exc:
        raise OpsCheckError(f"Cannot read rules {source}: {exc.strerror or 'I/O error'}.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OpsCheckError(f"Invalid UTF-8 JSON in rules {source}: {exc}.") from exc
    except (ValueError, InvalidOperation) as exc:
        if isinstance(exc, OpsCheckError):
            raise
        raise OpsCheckError(f"Invalid number in rules {source}.") from exc
    _check_rules(rules)
    return rules


def _check_limit(max_findings: int) -> None:
    if type(max_findings) is not int or max_findings < 0:
        raise OpsCheckError("max_findings must be a nonnegative integer.")


def _cell_type(value: str, column_type: str) -> tuple[bool, Decimal | None]:
    if column_type in {"number", "integer"}:
        pattern = _INTEGER if column_type == "integer" else _NUMBER
        if not pattern.fullmatch(value):
            return False, None
        try:
            number = Decimal(value)
        except InvalidOperation:
            return False, None
        return number.is_finite(), number
    if column_type == "email":
        return bool(_EMAIL.fullmatch(value)), None
    if column_type == "date":
        if not _DATE.fullmatch(value):
            return False, None
        try:
            date.fromisoformat(value)
        except ValueError:
            return False, None
    return True, None


def validate(dataset: Dataset, rules: dict[str, Any], max_findings: int = 500) -> dict[str, Any]:
    """Validate all records, retaining only the first requested findings."""
    columns = _check_rules(rules)
    _check_limit(max_findings)
    findings: list[dict[str, Any]] = []
    issues = 0
    affected: set[int] = set()

    def add(row: int | None, column: str, code: str, message: str, value: str) -> None:
        nonlocal issues
        issues += 1
        if row is not None:
            affected.add(row)
        if len(findings) < max_findings:
            findings.append({"row": row, "column": column, "code": code, "message": message, "value": value})

    active = {column: rule for column, rule in columns.items() if column in dataset.headers}
    for column in columns:
        if column not in dataset.headers:
            add(None, column, "missing_column", "Configured column is missing from the CSV header.", "")
    seen: dict[str, dict[str, int]] = {column: {} for column, rule in active.items() if rule.get("unique")}
    for record, row in enumerate(dataset.rows, start=2):
        for column, rule in active.items():
            original = row[column]
            value = original.strip()
            if not value:
                if rule.get("required"):
                    add(record, column, "required", "A nonblank value is required.", original)
                continue
            column_type = rule.get("type", "string")
            valid_type, number = _cell_type(value, column_type)
            if not valid_type:
                add(record, column, "type", f"Expected a valid {column_type} value.", original)
            if valid_type and number is not None:
                for bound_name in ("min", "max"):
                    if bound_name in rule:
                        boundary = _bound(rule[bound_name], bound_name)
                        outside = number < boundary if bound_name == "min" else number > boundary
                        if outside:
                            direction = "at least" if bound_name == "min" else "at most"
                            add(record, column, bound_name, f"Value must be {direction} {boundary}.", original)
            if "allowed" in rule and value not in rule["allowed"]:
                add(record, column, "allowed", "Value is not in the configured allowed list.", original)
            if rule.get("unique"):
                if value in seen[column]:
                    add(record, column, "duplicate", f"Duplicate value; first seen at record {seen[column][value]}.", original)
                else:
                    seen[column][value] = record
    return {
        "version": 1, "kind": "validate", "status": "fail" if issues else "pass", "source": dataset.source,
        "summary": {"rows": len(dataset.rows), "issues": issues, "affected_rows": len(affected), "reported_findings": len(findings)},
        "findings": findings,
    }


def _index(dataset: Dataset, key: str) -> dict[str, dict[str, str]]:
    if key not in dataset.headers:
        raise OpsCheckError(f"CSV {dataset.source} is missing key column {key!r}.")
    records: dict[str, dict[str, str]] = {}
    for record, row in enumerate(dataset.rows, start=2):
        value = row[key].strip()
        if not value:
            raise OpsCheckError(f"CSV {dataset.source}, record {record}: key {key!r} is blank.")
        if value in records:
            raise OpsCheckError(f"CSV {dataset.source}, record {record}: duplicate key {value!r}.")
        records[value] = row
    return records


def compare(before: Dataset, after: Dataset, key: str, max_findings: int = 500) -> dict[str, Any]:
    """Compare unique trimmed keys and exact shared non-key field values."""
    _check_limit(max_findings)
    if not isinstance(key, str) or not key.strip():
        raise OpsCheckError("Comparison key must be a nonblank column name.")
    old = _index(before, key)
    new = _index(after, key)
    before_headers, after_headers = set(before.headers), set(after.headers)
    schema = {"added": sorted(after_headers - before_headers), "removed": sorted(before_headers - after_headers)}
    shared_fields = sorted((before_headers & after_headers) - {key})
    counts = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0}
    findings: list[dict[str, Any]] = []
    for value in sorted(old.keys() | new.keys()):
        old_row, new_row = old.get(value), new.get(value)
        if old_row is None:
            kind, fields = "added", sorted(after_headers - {key})
        elif new_row is None:
            kind, fields = "removed", sorted(before_headers - {key})
        else:
            fields = [field for field in shared_fields if old_row[field] != new_row[field]]
            kind = "changed" if fields else "unchanged"
        counts[kind] += 1
        if kind != "unchanged" and len(findings) < max_findings:
            findings.append({
                "kind": kind, "key": value,
                "before": dict(old_row) if old_row is not None else None,
                "after": dict(new_row) if new_row is not None else None,
                "fields": fields,
            })
    total_changes = counts["added"] + counts["removed"] + counts["changed"]
    return {
        "version": 1, "kind": "compare",
        "status": "changed" if total_changes or schema["added"] or schema["removed"] else "unchanged",
        "source": f"{before.source} → {after.source}", "key": key,
        "summary": {"before_rows": len(before.rows), "after_rows": len(after.rows), **counts,
                    "total_changes": total_changes, "reported_findings": len(findings)},
        "schema": schema, "findings": findings,
    }
