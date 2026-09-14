# Project Journey

[README](../README.md) · [Architecture](ARCHITECTURE.md) · [Validation history](VALIDATION.md)

## Milestone 1 — Durable checks

Started with deterministic CSV validation and keyed snapshot comparison, then placed them in a fixed workflow with parallel specialists, verification, bounded retries, SQLite state and resume. An optional analyst/reviewer adapter explored model-assisted briefings while keeping the default path deterministic. The [original report](demo/report.html) is preserved as a historical snapshot.

## Milestone 2 — Human decisions

Added a durable approval boundary and a bounded rejection/revision loop. Briefing versions, feedback and decisions survive process restarts; rejection revises saved evidence without repeating completed specialists. A pending decision is state, not a terminal waiting for `input()`.

## Milestone 3 — Event identity

Added strict manifests, content fingerprints, permanent external-ID bindings, canonical run reservation, ingestion audit history, and one-shot scanning. Duplicate delivery reuses one workflow within a shared local store. Recovery connects receipt, claim, execution and human approval through durable identity.

## Milestone 4 — Worker ownership

Separated enqueue from execution with durable queue jobs, atomic claims, renewable leases, token fencing, delivery backoff, dead letters and explicit replay. Workers reuse the existing workflow and human lifecycle. The [Milestone 4 PR notes](MILESTONE4_PR.md) and validation record retain implementation evidence.

## Bugs that shaped the design

Windows exposed an SQLite cleanup error: a transaction context did not close its connection, leaving the database locked during temporary-directory deletion. Explicit connection ownership/closure and regression tests corrected it, including heartbeat and failure paths.

PR #3 review found that observer synchronization could clear a still-valid worker lease after the workflow committed a checkpoint. The fix preserves unexpired ownership and checks successful durable checkpoints before charging expired incomplete work as a delivery failure. Controlled-clock tests and a real subprocess completion race cover the boundary.

The project grew through these failures and corrections. Its evidence is the inspectable implementation, synthetic demos and tests; it does not establish distributed or production-customer reliability.
