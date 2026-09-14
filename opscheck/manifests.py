"""Strict, non-executable job manifests and content-based event identity."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PureWindowsPath

from .core import OpsCheckError
from .workflow import _json, fingerprint_from_hashes, workflow_config

SOURCE_FIELDS = ("input", "rules", "before", "after")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class Manifest:
    path: Path
    raw_hash: str
    document: dict
    paths: tuple[Path, ...]
    fingerprint: str
    workflow_config: dict
    workflow_fingerprint: str


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise OpsCheckError(f"Repeated manifest field: {key!r}.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise OpsCheckError(f"Non-JSON numeric constant: {value}.")


def read_document(path: Path) -> tuple[dict, str]:
    """Read a bounded regular file, rejecting duplicate keys and nonstandard JSON."""
    if not path.is_file():
        raise OpsCheckError("Manifest must be an existing regular file.")
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise OpsCheckError("Manifest exceeds the 64 KiB limit.")
    try:
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise OpsCheckError(f"Invalid manifest JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise OpsCheckError("Manifest must be a JSON object.")
    return document, hashlib.sha256(raw).hexdigest()


def candidate_sources(document: dict, directory: Path) -> list[Path]:
    """Protect named inputs even when schema/path validation subsequently fails."""
    paths = []
    for name in SOURCE_FIELDS:
        value = document.get(name)
        if isinstance(value, str) and value and "\0" not in value:
            paths.append((directory / value.replace("\\", "/")).absolute())
    return paths


def validate_manifest(path: Path, document: dict, raw_hash: str) -> Manifest:
    required = {"event_type", *SOURCE_FIELDS}
    allowed = required | {"key", "event_id"}
    missing = sorted(required - document.keys())
    if missing:
        raise OpsCheckError(f"Required field {missing[0]!r} is missing.")
    unknown = sorted(document.keys() - allowed)
    if unknown:
        raise OpsCheckError(f"Unsupported manifest field: {unknown[0]!r}.")
    for name, value in document.items():
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise OpsCheckError(f"Field {name!r} must be a nonblank string without NUL characters.")
    if document["event_type"] != "orders_check":
        raise OpsCheckError(f"Unsupported event_type: {document['event_type']!r}.")
    key = document.get("key", "order_id").strip()
    external_id = document.get("event_id")
    if len(key) > 256 or any(ord(char) < 32 for char in key):
        raise OpsCheckError("Invalid key: use at most 256 characters without control characters.")
    if external_id is not None and (len(external_id) > 256 or any(ord(char) < 32 for char in external_id)):
        raise OpsCheckError("Invalid event_id: use at most 256 characters without control characters.")
    directory = path.parent.resolve()
    paths, hashes = [], []
    for name in SOURCE_FIELDS:
        value = document[name].replace("\\", "/")
        relative = Path(value)
        if relative.is_absolute() or PureWindowsPath(value).drive or ":" in value:
            raise OpsCheckError(f"Field {name!r} must be a relative path beneath the manifest directory.")
        source = (directory / relative).resolve()
        if not source.is_relative_to(directory):
            raise OpsCheckError(f"Field {name!r} escapes the manifest directory.")
        if not source.is_file():
            raise OpsCheckError(f"Source file for {name!r} does not exist or is not a regular file: {value!r}.")
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(64 * 1024), b""):
                size += len(block)
                if size > MAX_SOURCE_BYTES:
                    raise OpsCheckError(f"Source {name!r} exceeds the 10 MiB limit.")
                digest.update(block)
        paths.append(source)
        hashes.append(digest.hexdigest())
    # Source roles and processing settings matter; locations, names and external
    # delivery IDs do not. Identical copies in another folder are the same job.
    identity = {"version": 1, "event_type": "orders_check", "key": key,
                "source_hashes": dict(zip(SOURCE_FIELDS, hashes))}
    fingerprint = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    config = workflow_config(paths, key=key)
    normalized = {**document, "key": key}
    return Manifest(path, raw_hash, normalized, tuple(paths), fingerprint, config,
                    fingerprint_from_hashes(paths, config, hashes))
