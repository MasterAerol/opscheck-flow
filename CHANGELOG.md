# Changelog

Notable changes are grouped below. No release dates or historical versions are assigned to milestones; no local version tags were present when this document was prepared. See [proposed release notes](docs/RELEASE_NOTES.md) and [validation history](docs/VALIDATION.md).

## Unreleased

### Added

- Deterministic CSV validation and keyed comparison with bounded, escaped JSON/HTML reports.
- Fixed workflow planning, parallel specialists, verification, persisted outputs, task retry and resume.
- Optional analyst/reviewer model adapter with bounded review rounds and evidence-ID validation.
- Durable human approval, immutable briefing/decision history and deterministic revision.
- Strict event manifests, content-based idempotency, permanent external-ID bindings, canonical run reservation and ingestion inspection/recovery.
- Local SQLite worker queue with atomic claims, renewable leases, stale-token fencing and persisted attempts.
- Crash recovery, delivery backoff, dead-letter inspection and replay retaining job/event/run identity.
- Windows/Linux CI, deterministic concurrency and crash tests, and installed-package lifecycle smoke coverage.
- Portfolio, interview, recording, journey and proposed release documentation; a compact README and detailed CLI reference.

### Fixed

- Explicit SQLite connection closure on normal, initialization-error and background heartbeat paths, preventing Windows temporary-database cleanup failures.
- Observer synchronization no longer clears an active worker lease. Expired successful checkpoints reconcile before incomplete-delivery expiration; regressions cover inspection, polling and process completion races.

### Clarified

- Local coordination guarantees and limitations, deterministic versus optional model roles, separate retry budgets, and the distinction between queued reservation and workflow execution.
- Historical validation records remain evidence of their original runs; current local validation and hosted CI have separate sources.
