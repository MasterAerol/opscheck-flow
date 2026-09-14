# OpsCheck Flow — Portfolio Summary

[README](../README.md) · [Architecture](ARCHITECTURE.md) · [Interview guide](INTERVIEW_GUIDE.md) · [Demo](DEMO_GUIDE.md)

## One-line description

A Python/SQLite workflow runner that checks CSV exports and persists the event, worker, recovery, and human approval lifecycle.

## Problem

Recurring operational exports need quality checks, snapshot comparison, a verified briefing, and a human decision. Duplicate submissions and interrupted processes should not silently create new workflows or discard completed work. This project models that problem with synthetic orders; it does not claim customer deployment or business savings.

## What I built

A local CLI and library with deterministic parallel specialists, a verifier, persisted task outputs, and bounded retries. Strict JSON manifests create canonical events and reserve workflow IDs. A SQLite queue separates receipt from execution; workers claim renewable leases, recover interrupted jobs, and retain exhausted deliveries for explicit replay. A briefing pauses at a saved approval boundary and can be revised from verified evidence before approval.

This is suggested application wording: use it only for work you understand and can explain. Describe your own contribution and any AI assistance accurately. The [project journey](PROJECT_JOURNEY.md) includes bugs and corrections, and the [validation record](VALIDATION.md) separates executed checks from claims.

## Engineering patterns demonstrated

| Area | Concrete evidence |
| --- | --- |
| Orchestration and concurrency | Fixed DAG; independent quality/change tasks overlap through a thread pool |
| Persistence and retries | SQLite task outputs and attempt history; bounded recovery of incomplete work |
| Verification and agent/reviewer loops | Deterministic verifier; optional model analyst/reviewer with evidence validation and bounded rounds |
| Human approval | Saved briefing versions, conditional decisions and deterministic revision |
| Event processing and idempotency | Content fingerprints, permanent external-ID bindings and canonical run reservation |
| Local worker queue | Atomic claims, renewable leases, token fencing and backoff |
| Crash recovery and dead letters | Reuse saved checkpoints; preserve job/event/run IDs and replay history |
| Testing and CI | Deterministic clocks/barriers, real process races/crashes, Windows/Linux matrix and installed smoke |

## Technical stack

Python 3.10+, SQLite, and the standard library (`concurrent.futures`, `sqlite3`, `argparse`, `unittest`, HTTP and filesystem primitives). Standalone HTML/JSON reports require no web server. GitHub Actions runs the cross-platform checks. Setuptools builds the package; build tools are development dependencies, and the installed runtime has no third-party dependencies. No major orchestration framework is required.

## Design decisions

| Decision | Reason and tradeoff |
| --- | --- |
| SQLite | Transactions and uniqueness constraints provide durable local coordination without operating a service; this limits the supported coordination scope |
| Deterministic workers | CSV findings remain reproducible and testable; optional model prose is kept separate from data facts |
| Persist approval instead of `input()` | The process can exit and another invocation can decide the exact saved version |
| Content fingerprints | Identical content can reuse a canonical event despite delivery filename or external-ID changes |
| Expiring leases | Another local worker can recover after an owner dies |
| Lease tokens | Conditional mutations distinguish the current owner from an expired worker, even when worker names repeat |
| Dead letters | Bounded failure budgets stop automatic retries while retaining diagnostics and operator replay |

The [active-lease regression](PROJECT_JOURNEY.md#bugs-that-shaped-the-design) is a useful design example: a committed workflow checkpoint does not authorize an observer to clear a live worker's lease.

## What I would change for distributed production use

First define delivery guarantees, workload size, failure domains, and operational requirements. Then evaluate a shared database such as PostgreSQL or a managed durable store, a real message broker with visibility timeouts, and a distributed ownership design. Add authenticated producer/reviewer identities, authorization, secrets management, metrics/tracing, alerting, deployment/containerization, backup/restore exercises, and a versioned schema-migration strategy. External side effects would need their own idempotency and reconciliation contracts.

These are future design considerations, not existing features or a claim that swapping the database alone makes the system distributed.

## Resume bullets

- Built a Python/SQLite workflow runner with parallel CSV checks, idempotent event ingestion, durable human approval, worker lease recovery, and dead-letter replay.
- Implemented concurrency safeguards with atomic SQLite claims, OS process locks, lease-token fencing, and persisted attempt history; regression-tested worker races and crash recovery.
- Added Windows/Linux CI and installed-package smoke coverage for retry, ingestion, queue, revision, and approval lifecycles, with separate source-distribution and wheel validation.

## 30-second interview explanation

“OpsCheck Flow models reviewing recurring CSV exports. It runs quality and change checks in parallel, verifies the evidence, and pauses for a saved human decision. The interesting part is recovery: duplicate manifests reuse one run, workers use renewable leases, and failed deliveries retain their history through dead-letter replay. It uses Python and SQLite locally. The default workers are deterministic; model-assisted briefing is optional.”

## 2-minute interview explanation

“The scenario is a new orders export that needs validation and comparison before an operator acts. I modeled the whole review lifecycle: a fixed plan starts two independent specialists, a verifier checks their outputs, and a briefing is saved for human approval.

The state boundaries are deliberate. Completed tasks commit their outputs to SQLite, so recovery can reuse them. Approval is a persisted versioned record, so the CLI can exit and the operator can reject or approve later. Rejection runs bounded deterministic clarification against saved evidence and creates another approval version.

For recurring input, strict manifests are fingerprinted by source content and processing identity. A duplicate finds the same canonical event and reserved run. Enqueue persists a job without running specialists. Workers claim atomically, renew a lease, and settle only with their current token. If one dies, another process can recover after expiration. A saved approval checkpoint is reconciled without treating it as a failed delivery; incomplete work uses the retry budget. Exhaustion becomes an inspectable dead letter, and replay keeps the same identities and history.

One concurrency bug made that boundary clearer: an inspector could clear a valid lease when it saw a completed workflow checkpoint. The fix protects active owners and reconciles successful checkpoints only after expiration. Deterministic process synchronization now exercises that race.

The guarantee is local coordination in one shared SQLite store, not global exactly-once execution. Tests cover crashes, races, stale tokens and Windows connection cleanup. I would need a different ownership and operational design for multi-machine production. No production adoption or live-model quality is claimed.”
