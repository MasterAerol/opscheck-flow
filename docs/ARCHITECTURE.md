# Architecture

OpsCheck Flow separates a useful deterministic data tool from the orchestration around it. This keeps the workflow inspectable: every data-quality conclusion comes from a rule or a comparison, and optional model recommendations refer to that evidence.

## Modules and boundaries

| Module | Responsibility |
| --- | --- |
| `opscheck/core.py` | CSV parsing, rule validation, data validation, and keyed comparison |
| `opscheck/workflow.py` | Plan, concurrent task scheduling, verification, retries, SQLite state, resume, and run locks |
| `opscheck/approvals.py` | Approval schema, atomic decisions, durable checkpoints, revision recovery, and inspection |
| `opscheck/revision.py` | Deterministic clarification using verified evidence and human feedback |
| `opscheck/artifacts.py` | Input protection and atomic report replacement under the run lock |
| `opscheck/agents.py` | Bounded evidence, local chat-completions transport, analyst/reviewer role contexts, JSON validation, and revision loops |
| `opscheck/report.py` | Human-readable summaries and escaped standalone HTML |
| `opscheck/cli.py` | CLI arguments, exit codes, demo resources, destination protection, and report writes |
| `opscheck/examples/` | Bundled synthetic CSV fixtures and a JSON rule set |
| `tests/` | Engine, CLI, workflow/recovery, and model-adapter behavior tests |

The `python -m opscheck` entry point invokes the CLI. Source execution needs no installation, and installed packages include the demo resources.

## Planning and parallel execution

The planner emits a fixed dependency graph. It declares what must finish before each task is eligible to execute; it does not use a model to choose tools dynamically.

```mermaid
flowchart TD
    P[Planner] --> Q[Quality worker]
    P --> C[Change worker]
    Q --> V[Verifier]
    C --> V
    V --> B[Briefing]
    B --> G[Approval gate]
    G -->|Approve| S[SUCCEEDED]
    G -->|Reject within budget| R[Revision agent]
    R --> G
    G -->|Limit reached| F[FAILED]
```

`quality_agent` and `change_agent` are independent Python workers scheduled through `ThreadPoolExecutor`. Their names describe responsibilities; they are not model calls. Each produces a structured result. The verifier waits for both and confirms result shape, count consistency, source identity, and equality with a fresh deterministic computation.

The verifier uses the same core engines, so shared engine bugs remain possible. Its purpose is to catch inconsistent outputs and orchestration mistakes, not to act as an independently implemented proof of correctness. Concurrency tests use barriers to establish overlap rather than comparing wall-clock benchmarks.

## Task state and bounded recovery

A task may be `pending`, `running`, `succeeded`, `failed`, or `blocked`. Ready tasks start; a successful task unlocks its dependents. A non-recoverable failure, or a transient failure that exhausts the allowance, blocks downstream tasks while preserving successful independent work.

Retryable failures include explicit demo fault injection and temporary model transport failures. Invalid CSV, invalid rules, malformed model output, or exhausted model review are not fixed by blindly repeating the same operation.

`max_attempts` is limited to 1–5, defaulting to 2. This is an attempt allowance per task for the current invocation. Resume provides another bounded invocation while preserving total lifetime attempts. No retry loop is unbounded and no background scheduler runs after the CLI exits.

Important events include `task_started`, `task_succeeded`, `task_failed`, `task_retrying`, and `task_reused`. Ordered events expose what occurred, including a failed first attempt that was later recovered. Timestamps describe execution history; they are not a performance guarantee.

## Persistence, identity, and locks

The default database is `.opscheck/runs/runs.sqlite3`.

| Table | Stored information |
| --- | --- |
| `runs` | Run ID, configuration, fingerprint, explicit run status, failure reason, and timestamps |
| `tasks` | Task states, accumulated attempts, errors, and completed outputs |
| `events` | Ordered event history, task IDs, timestamps, and event details |
| `approvals` | Version, decision, reviewer/comment, timestamps, briefing reference, immutable briefing JSON |

Run IDs are generated UUID hex strings. Completing each task persists its output and state. Resume skips successful tasks and resets interrupted `running` tasks so they can be attempted again. Failed downstream work can continue after its dependency recovers.

The resume fingerprint includes absolute input paths, file contents, the comparison key, model endpoint/name, maximum model rounds, workflow version, and human rejection budget. Changed source/configuration identity is rejected; start a new run for new exports. Recovery controls such as `max_attempts` and `fail_once` are excluded so they can change on resume without redefining the data being analyzed.

An OS advisory lock prevents concurrent execution of the same run. Locks automatically release when a process exits or crashes. The small `.lock` file remains as an inert marker; its presence does not mean the run is still executing. **Do not delete lock files to recover a run.** Deleting a marker while another process holds its lock can undermine mutual exclusion. Run locking supports POSIX and Windows systems.

SQLite is local state, not distributed coordination. v0.1 does not provide a queue, cross-machine worker leasing, or an encrypted data store.

## Optional model subagents

Default briefing generation uses code. When a loopback model server is explicitly configured, the briefing step calls an analyst and a reviewer with separate role contexts. They may use the same underlying model; separate contexts do not guarantee independent judgment.

```mermaid
flowchart TD
    E[Verified evidence] --> A[Analyst]
    A --> S[Schema and evidence checks]
    S --> R[Reviewer]
    R -->|Approved| F[Final briefing]
    R -->|Revise within limit| A
    R -->|Limit reached| X[Task fails]
```

`build_evidence()` preserves aggregate summaries and assigns stable IDs such as `V1` for a validation finding and `C1` for comparison evidence. Schema changes receive comparison evidence too. The adapter considers at most 20 findings per tool, bounds strings and collections, and caps the evidence payload at 64 KiB. Truncation metadata distinguishes a sampled briefing from complete tool totals.

The analyst returns a JSON object with `summary` and `actions`. Every action has a title, a supported priority, and valid evidence IDs. The reviewer returns an approval decision and feedback. Rejection can lead to a revised analyst response; the limit is 1–3 rounds, defaulting to 2. A completed round normally uses two model calls. Transient transport retry can cause the enclosing briefing task to restart within the workflow's attempt limit, so total calls across retries may exceed one review loop.

Malformed envelopes, invalid JSON, unknown evidence IDs, and unacceptable schemas fail the task. Even valid evidence IDs do not establish that prose is correct: an action can cite a real finding and still misinterpret it. Model reviewer approval remains a fallible judgment.

The adapter accepts only an HTTP(S) loopback URL ending in `/v1/chat/completions`. It disables redirects and environment proxy use. Requests have a 30-second timeout; response bodies are limited to 128 KiB. The selected server must understand the chat-completions request format, including JSON-object response format. No model is downloaded automatically. Models have no tool-execution capability in this project.

A fake local HTTP server tests the transport and response-handling behavior. That does not establish compatibility or quality for every real local model server. The initial build did not run a live local model.

## Core engine semantics

CSV decoding accepts UTF-8 and an optional BOM. The loader rejects missing/blank/duplicate headers, malformed rows, width mismatches, files over 10 MiB, and more than 100,000 records. A header-only CSV is valid. Rows are represented as dictionaries of strings; no source data is mutated.

Validation loads a strict versioned JSON schema. It trims outer value whitespace for checks and uses `Decimal` for finite numeric comparisons. Optional blanks skip their rules. Unique fields retain a set of seen nonblank values and report subsequent duplicates. A configured missing column is a schema finding, even when its cells would be optional. Rule findings preserve their original source value and logical CSV record number.

Comparison indexes each snapshot by a unique nonblank trimmed key. Shared non-key values are compared exactly. Changes are recorded once per key, with a list of changed fields; totals count records rather than cells. Added/removed columns are separate schema changes and do not inflate changed-record counts. Keys are sorted for deterministic output.

The engines count every issue/change even if detailed findings are capped. The default cap is 500. This bounds result size, not processing time, and input datasets are still held in memory.

## Reports and exit status

Standalone tools can emit text, JSON, and HTML. Flow commands persist a JSON run report, an HTML workflow report, and tool HTML reports when their results are available. Individual report writes use temporary files and replacement. This does not make all report files a single atomic transaction; an I/O failure may occur after another report was written.

Input/configuration paths are protected against report overwrite, including aliases through symlinks and hardlinks. Distinct requested reports must have distinct destinations. Source values and model text are escaped in HTML. Reports contain source values and should be treated with the same care as their inputs.

Tool status and workflow status answer different questions:

- `validate` fails its data check when findings exist and exits `1`.
- `compare` reports changed records or schema and exits `1`.
- A workflow can succeed with those results because every step executed, verified correctly, and received human approval; it exits `0`. A durable approval pause also exits `0`.
- Invalid input, execution failure, or I/O error yields CLI exit `2`.

A workflow status of `SUCCEEDED` is never permission to apply a business action. v0.1 produces reviewable evidence and briefings; it does not change source records or contact external business services.

## Durable human decision boundary

`PLAN` retains the existing executable DAG of planner, parallel specialists, verifier, and briefing. The conditional approval/revision state machine follows that DAG rather than pretending a human decision is a worker future. `approval_gate` is recorded as a task with `waiting`, `succeeded`, or `failed` status. Each attempted revision has its own `revision_agent_N` task record. The public plan includes both conditional roles.

Run state uses `PENDING`, `RUNNING`, `WAITING_FOR_APPROVAL`, `SUCCEEDED`, and `FAILED`; task state remains lowercase. New runs are created pending and transition to running before execution. Completed workers no longer finalize a run. After the briefing output commits, the approval state machine atomically inserts the pending approval and its exact briefing JSON, records `approval_requested` and `workflow_paused`, and sets the run waiting. No terminal input loop or background process remains.

An approval decision transaction updates only the observed pending ID using a conditional update. The same transaction stores the decision, identity, comment, timestamp, `approval_approved`/`approval_rejected`, `workflow_resumed`, and the run's `RUNNING` state. Approval then marks the gate and run successful. Rejection starts a focused deterministic revision against saved worker/verifier evidence and the rejected briefing. Its output commits before the next approval request is inserted.

Crash boundaries are deliberate:

| Last committed state | Recovery |
| --- | --- |
| Briefing completed, no request | Create the first pending request |
| Pending request | Leave waiting; ordinary resume cannot approve |
| Approved decision, run running | Finalize success without rerunning specialists |
| Rejected decision, revision not completed | Execute or retry that revision |
| Revision output saved, next request absent | Reuse the saved output and request the next approval |
| Decision committed, report write failed | Regenerate reports from SQLite using `resume` |

The default `max_human_revisions=3` means a maximum of three rejected versions, including the original. The third rejection fails the gate/run, persists a clear failure reason and `revision_limit_reached`, and creates no further request. This failure is terminal across resume. A revision execution error is saved separately and can be retried with resume. Normal worker retries remain bounded per invocation and independent of the human rejection budget and optional model review budget.

`revision_started` and `revision_completed` events include task, iteration, and approval ID. Each successful revision retains the original summary, records feedback, orders validation or change detail first, and includes exact saved finding samples with aggregate totals and sampling disclosures. It does not revalidate changed source files, invent missing records, perform source repairs, or call an external service. A model-generated original briefing retains its original model review history; the new revision metadata explicitly identifies deterministic local revision.

## Approval transactions and concurrency

SQLite `BEGIN IMMEDIATE` serializes schema migration and decision writes. A partial unique index allows only one pending approval per run; `(run_id, iteration)` is unique. Triggers prevent changing resolved decisions, changing briefing identity/content, or deleting approval history through ordinary SQL. A local database owner can still alter the schema; this is not a tamper-proof compliance store.

The existing OS run lock spans decision execution, revision computation, and report regeneration. It prevents two processes from writing competing reports or executing the same revision. Before taking this lock, a decision snapshots the current pending ID; the transaction must still match that exact ID and pending status. `--approval-id` additionally lets a delayed client bind a decision to the version actually reviewed. Without an explicit ID a newly started command intentionally observes the currently pending version. A losing concurrent command fails cleanly; it never redirects an observed old decision onto a new version.

All SQLite connections close in `finally` blocks; a connection's transaction context alone is insufficient. Reports are rendered from committed state and replaced atomically per file while holding the run lock. They are not a transaction across all files. `inspect_run` reads a SQLite snapshot and does not trust reports. Individual approval snapshots remain in SQLite even if all report files are deleted.

## Existing database compatibility

Opening a Milestone 1 database adds the approval table/indexes/triggers and nullable failure reason, and normalizes run statuses to uppercase without changing task evidence. Legacy fingerprint verification continues to use the saved version-1 configuration. Already completed historical runs remain completed and do not gain fabricated approvals. An unfinished legacy run gains the approval gate after completing its briefing, with the default three-rejection budget. No general migration framework for future incompatible schemas is claimed.

Human reviewer names are asserted local metadata, not authenticated accounts. SQLite and locks coordinate one local filesystem; network filesystem and distributed operation are not supported. HTML escapes human comments, reviewer names, source evidence, and failure reasons. The optional model reviewer remains a separate boundary and can never grant human approval.
