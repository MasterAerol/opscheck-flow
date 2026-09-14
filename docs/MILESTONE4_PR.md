# Add durable worker queue, leasing, and dead-letter recovery

## Summary

Adds a durable local worker queue to OpsCheck Flow with atomic job claiming, expiring leases, heartbeat renewal, crash recovery, delivery retries, dead-letter handling, and replay.

## What changed

- Added persistent queue jobs and attempt history
- Added atomic SQLite job claiming
- Added worker lease tokens and expiration
- Added heartbeat-based lease renewal
- Added stale-worker protection
- Added local worker CLI modes
- Added bounded delivery retries and deterministic backoff
- Added dead-letter handling and replay
- Added queue inspection commands
- Added queue/workflow/event synchronization
- Preserved existing synchronous ingestion and approval flows
- Added escaped queue metadata to reports
- Added installed-package queue lifecycle smoke coverage

## Architecture

Event → Queue → Lease → Worker → Existing Workflow → Human Approval → Completion

Crash recovery:

LEASED → lease expires → another worker reclaims → same queue job → same event → same reserved workflow run

## Delivery semantics

OpsCheck provides durable local multi-process queue coordination using one shared SQLite state store. It does not claim distributed exactly-once delivery across independent machines.

A queue job retains its canonical event/run identity across duplicates, retries, crashes, and replay. Token-checked lease writes reject stale workers, while existing event/run locks prevent simultaneous workflow mutation. Replay resets only the delivery budget; lifetime attempts and approval history remain intact. Human revision-limit failures cannot be replayed.

## Validation

- Complete suite on Windows Python 3.12.10: **217 tests, 215 passed, 0 failed/errors, 2 intentional Windows symlink skips**. All 174 prior tests are retained; 43 queue tests were added.
- Enqueue: one canonical event/job/reservation, QUEUED, zero workflow rows and zero tasks.
- Duplicate enqueue before processing: same event/job/run reservation, no new workflow or queue job; reuse explicitly reported.
- Worker claim: original reserved run reached WAITING_FOR_APPROVAL; event/job matched and lease cleared.
- Two-worker race: deterministic subprocess synchronization yielded one delivery and one EMPTY result; one event/job/run.
- Heartbeat: expiry advanced, and competing claims were excluded beyond the original expiration under a controlled clock.
- Stale lease: heartbeat, completion, failure, and requeue attempts were rejected without overwriting the replacement owner.
- Crash recovery: a real process exited after claim; after actual five-second expiry, another CLI worker recovered the same job/event/run on attempt 2. Automated crash-boundary and live run-lock deferral tests also passed.
- Retry/backoff: errors and attempts persisted; actual CLI failures requeued with 1/2-second retry delays.
- Dead-letter: three failed deliveries produced DEAD_LETTER with retained error, timestamp, every attempt, and audit evidence; inspection passed.
- Replay: same job/event/run, replay_count incremented, original history retained, valid new budget, fourth lifetime attempt reached approval.
- Approval lifecycle: normal and replayed jobs reached queue SUCCEEDED, event COMPLETED, workflow SUCCEEDED. A separate rejection ran revision_agent_1, kept the same job/run and delivery count, then approved successfully. Specialists did not rerun unnecessarily.
- Windows SQLite cleanup: database deletion passed after ten lifecycle boundaries, including duplicate enqueue and background heartbeat renewal.
- Package build: source distribution and wheel built successfully.
- Installed-package smoke: a fresh environment physically outside the checkout imported site-packages; both successful worker --once and retry/backoff flows reached final queue/event/workflow completion.
- git diff --check: passed. Intended changes were reviewed; runtime state, databases/sidecars, reports, locks, caches, environments, and build artifacts were excluded. Credential-pattern checks found no matches.

## Limitations

- Local SQLite coordination only
- No distributed consensus
- No network queue server
- No multi-machine lease guarantee or network-filesystem guarantee
- Worker IDs are diagnostic metadata, not authentication
- Lease timing assumes a sufficiently consistent local clock
- Lease loss does not forcibly cancel an already running specialist; the run lock remains the execution boundary, and incomplete work may legitimately retry after a crash
- Live model execution was not tested; existing fake-model/adapter tests passed
- Long-running worker mode is implemented; prolonged unattended operation was not tested
- Ubuntu and other supported Python versions await CI validation; no remote Milestone 4 GitHub Actions result is claimed
