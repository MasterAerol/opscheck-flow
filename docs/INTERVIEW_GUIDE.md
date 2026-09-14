# Interview Guide

[Portfolio](PORTFOLIO.md) · [Architecture](ARCHITECTURE.md) · [Demo](DEMO_GUIDE.md)

Use these as study prompts. Trace the referenced implementation and explain the decisions in your own words. Be accurate about personal contributions and AI assistance; memorizing answers is not a substitute for understanding the code.

## Why not just use a single script?

A one-off check can be a script. Recurring review needs persisted progress, duplicate handling, delayed decisions, retry limits, and crash recovery. OpsCheck makes those boundaries visible. The extra machinery is justified by that modeled lifecycle, not by CSV parsing alone.

## Why SQLite?

It supplies local transactions, unique constraints and durable records with no service dependency. `BEGIN IMMEDIATE` serializes competing writers for claims and decisions. It is appropriate to the supported shared local store; it does not provide distributed consensus. Inspect [queue_store.py](../opscheck/queue_store.py) and [workflow.py](../opscheck/workflow.py).

## What does idempotency mean here, and how are duplicates prevented?

At most one canonical workflow exists per event fingerprint within one shared state store. The fingerprint contains the identity-format version, event type, normalized key and hashes of the four input roles. Paths, delivery filenames and external IDs are excluded. SQLite uniqueness and atomic run reservation enforce reuse. An external ID is permanently bound to its original fingerprint; different content under that ID is rejected. Changing rule bytes changes identity, even if the rules mean the same thing. See [manifests.py](../opscheck/manifests.py) and [event_store.py](../opscheck/event_store.py).

## What happens if two workers claim the same job?

The write transaction selects an eligible job and persists its lease and attempt before commit. Only one contender claims that job; the other finds no eligible work or another job. OS event/run locks separately protect actual workflow execution. A worker encountering an existing execution lock defers safely and refunds that deferral's delivery-budget charge. The test suite coordinates real processes using barriers.

## What is a lease, and why is its token important?

A lease is time-limited ownership with an owner name, expiry and random token. Heartbeats conditionally extend it. Completion, failure and requeue must match the current token and an unexpired LEASED row. The name is diagnostic metadata; the token distinguishes different ownership generations. A stale worker cannot overwrite its successor. See [queue.py](../opscheck/queue.py) and [worker.py](../opscheck/worker.py).

## Can an inspector settle a job after the workflow saves its briefing?

Only after the lease expires. A live owner's lease stays authoritative even when the workflow is already waiting or successful. The PR #3 regression fix protects inspection, list, replay and competing claim paths. An expired successful checkpoint is reconciled before incomplete expiration is charged as a failed delivery; normal successful attempts retain `lease_lost = false`.

## How does crash recovery work?

SQLite preserves identity, attempts and task checkpoints; OS locks release on process death. Queue ownership becomes reclaimable after expiry. A waiting/successful checkpoint settles the same attempt without rerunning the workflow. Incomplete expired work is recorded EXPIRED and requeued or dead-lettered according to budget. Recovery keeps the same job, event and reserved run. A task whose output never committed may run again: this is not exactly-once execution of every instruction.

## Why persist approvals, and what happens after rejection?

The exact briefing version, decision and reviewer metadata survive process restarts. A conditional update resolves only the pending version observed by the decision command; `--approval-id` can pin a delayed client's version. Rejection runs `revision_agent_N` using saved verified evidence, then asks for a new decision. Completed specialists are reused. The default budget is three rejected versions, including the original, which permits at most two revisions before terminal failure. See [approvals.py](../opscheck/approvals.py) and [revision.py](../opscheck/revision.py).

## What is the difference between task retry and queue retry?

Task retry is a bounded allowance inside one workflow invocation, default 2 attempts per task. Queue retry is a new delivery of the same job, default budget 3 claims, with persisted availability and deterministic exponential backoff capped at 60 seconds. Completed task outputs can survive both. Lifetime delivery attempts and the current replay budget are separate counters. Data-quality findings are business results, not delivery errors.

## What is dead-lettering, and what does replay reset?

An exhausted or terminal delivery becomes an inspectable DEAD_LETTER with its error and attempt history. Explicit replay of a replayable job creates a new delivery budget and increments replay count; it preserves job/event/run IDs and original attempts. It cannot reset exhausted human revision limits or repair immutable identity conflicts.

## Why is this not distributed exactly-once processing?

Independent stores have independent identities. Coordination relies on local SQLite, OS locks, and a consistent local clock; it does not cover separate machines or network filesystems. A process can die after computing work but before committing its result, so incomplete computation may repeat. External side effects are not implemented and would need their own idempotency contracts.

## Which components actually use an LLM?

Only the optional briefing analyst and reviewer, with separate role contexts and bounded rounds over sampled evidence IDs. The planner, specialists, verifier, default briefing and human revision are deterministic Python. Model review cannot authorize the human gate or execute tools. Fake HTTP-server tests verify transport/schema behavior; they do not establish real-model quality. See [agents.py](../opscheck/agents.py).

## What happens if a report write fails?

The committed SQLite decision/checkpoint remains authoritative. Fix the destination problem and resume the run to regenerate derived reports. Do not resubmit an already committed decision. HTML files are replaced atomically individually, not as one multi-file transaction.

## What would need to change for cloud production?

Define failure domains and side-effect semantics, then design distributed storage/queue ownership, authentication, authorization, observability, backups, migrations and deployment. Validate those guarantees under failure. None is implied by the current local implementation; see the [portfolio tradeoffs](PORTFOLIO.md#what-i-would-change-for-distributed-production-use).
