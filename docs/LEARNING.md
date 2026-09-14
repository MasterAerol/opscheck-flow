# Learn by owning the project

[README](../README.md) · [Current demo](DEMO_GUIDE.md) · [Interview guide](INTERVIEW_GUIDE.md) · [Project journey](PROJECT_JOURNEY.md)

This study path originated with Milestone 1. The historical exercises below are retained with their current implementation status; use the demo guide for the complete queued approval lifecycle.

This guide turns an AI-assisted starter project into practical experience. The aim is to be able to explain a decision, reproduce a failure, improve the implementation, and show evidence that your change works.

Running generated code once is only the starting point. Work through the steps with your editor open and keep a short engineering log of what you changed, what surprised you, and how you verified it.

## 1. Understand the operations problem

Read the five small fixtures in `opscheck/examples/`. Before running anything, manually predict the eight validation issues and the four changed comparison records. Write down why a missing email differs from an invalid email and why two changed fields on one order count as one modified record.

Then run:

```bash
python -m opscheck demo
```

Open both generated HTML files. Match each finding to the source CSV. Explain why `ORD-1002` is a duplicate only after its first occurrence in the messy file, and why the `ORD-1006` price/status update is one modified record in comparison.

**You should be able to explain:** input formats, rules, unique keys, deterministic results, and the difference between a record count and a field count.

## 2. Trace one result through the code

Follow a validation command from `__main__.py` through the CLI into `load_csv()`, `load_rules()`, `validate()`, and the report renderer. Use a debugger or a temporary print while learning, then remove it before committing.

Try a header-only CSV, a quoted comma, a duplicate header, and a malformed date. Predict whether the outcome should be a valid empty dataset, a finding, or an input error before running the command. Locate the tests for that behavior.

**You should be able to explain:** parsing versus validation, exceptions versus returned findings, data structures, `Decimal`, and exit codes `0`, `1`, and `2`.

## 3. Observe parallel workers and recovery

```bash
python -m opscheck flow-demo --fail-once quality_agent
python -m opscheck runs
```

Open the generated workflow report and locate the first failure, retry, and later success. Identify which task could continue independently. Resume the printed run:

```bash
python -m opscheck flow-demo --resume RUN_ID
```

Replace `RUN_ID` with your actual run ID. Find the `task_reused` events and compare attempt counts. The reused tasks should not execute again. Read the workflow tests that use a barrier and explain why a barrier provides stronger evidence of overlap than a very short elapsed time.

For crash/failure recovery, use the explicit `flow` command in the [CLI guide](CLI_GUIDE.md#run-it-on-your-files) with `--fail-once quality_agent --max-attempts 1`; then resume that run. Change a copied input file before resuming another run and confirm that the fingerprint rejects it. Use a copy rather than modifying the bundled fixtures.

**You should be able to explain:** dependency graphs, task state transitions, temporary versus permanent failures, bounded retries, persistence, identity checks, and why OS lock files are not deleted for recovery.

## 4. Distinguish tool workers from model subagents

Read `agents.py` and its tests. The default quality/change workers are Python functions. Model mode adds an analyst context and a reviewer context after the deterministic evidence has been verified.

Inspect a fake-server test that requests a revision and then approves the next answer. Trace the two rounds and check where evidence IDs are validated. Also inspect a rejected invented-ID response and an exhausted-review case. No real model server is required for these tests.

If you already have a compatible local server, run the optional model command using synthetic data. Record its model name, server version, observed output, and a case where it gives a weak recommendation. Do not describe fake-server tests as a successful live-model integration.

**You should be able to explain:** separate role contexts, structured model output, evidence grounding, review loops, sampling, transport errors, and why reviewer agreement does not prove factual correctness.

## 5. Make and defend one real change

Pick one exercise below. Write the expected behavior first, implement it on a branch, test the important edge cases, and write a small pull request. Ask someone to review it if possible. Your most useful portfolio evidence is the diff you can explain and the failure you can reproduce.

### Exercise A: Add a cross-field rule

Support a rule such as `paid_at` must be present when `status` is `paid`. Define a versioned JSON shape that uses fixed supported operators; do not use `eval()` or executable rule strings.

Acceptance checks: a paid order without `paid_at` fails, a pending order without it passes, whitespace is handled consistently, missing referenced columns produce a clear error/finding under your documented policy, and unknown operators are rejected. Keep existing per-column rules compatible. Explain why this rule cannot be expressed by marking `paid_at` unconditionally required.

Skills: schema design, compatibility, input validation, and testing behavior rather than copying the implementation into tests.

### Exercise B: Add a human approval gate

**Implemented in Milestone 2.** Treat this original exercise as a design-review prompt: trace `approvals.py`, reproduce the revision demo, and inspect the existing concurrency tests before proposing another change.

Introduce a review step before a generated briefing is marked ready to act on. A human should approve or reject a specific run's verified evidence through a CLI command. This exercise should record a decision locally; it should not send email, edit business files, or perform a real external action.

Acceptance checks: the workflow pauses with a clear pending-approval state, the decision is recorded, resume continues only after approval, rejection is visible, changing inputs requires a new approval, and concurrent commands cannot approve different evidence under the same run ID. Design how old run state remains readable.

Skills: state-machine design, audit history, authorization boundaries, concurrency, and migration planning.

### Exercise C: Add scheduled folder processing with idempotency

**Partly implemented:** Milestone 3 provides strict-manifest ingestion and one-shot scanning; Milestone 4 adds enqueue and worker polling. Review those interfaces first. Time-based scheduling and arbitrary CSV discovery remain proposals, not current capabilities.

Build a separate command that scans a user-selected folder once, recognizes new CSV snapshots, and schedules a workflow per new content fingerprint. Run it manually first; integrate with an operating-system scheduler only after its one-shot behavior is reliable.

Acceptance checks: unchanged files do not generate duplicate runs, a new content version creates a new run, interrupted scanning can recover, malformed inputs do not stop unrelated files, partially copied files are handled by an explicit policy, and concurrent scans cannot duplicate the same job. Keep timeouts and retry counts bounded.

Skills: idempotency, event-driven work, filesystem edge cases, queue/state design, and operational reliability.

## A five-minute interview demonstration

1. **Problem, 30 seconds:** “Teams receive CSV exports and need to know whether they are usable and what changed. This project checks the export and keeps an inspectable history of the workflow.”
2. **Live workflow, 60 seconds:** Run `flow-demo --fail-once quality_agent`. Show parallel tasks, the deliberate failure, bounded retry, and the final report.
3. **Recovery, 45 seconds:** Resume the same run and point to reused outputs and preserved attempt counts.
4. **Engineering choice, 60 seconds:** Explain the fixed plan, SQLite state, exact comparison semantics, and why recomputation checks consistency but does not remove all engine bugs.
5. **Subagents, 45 seconds:** Show the optional analyst/reviewer test or a real model run you have personally completed. State clearly which one you are demonstrating.
6. **Your contribution, 60 seconds:** Show a change you made, the original failure, the regression test, and its tradeoff. Be specific about how AI helped and what you reviewed yourself.

An interviewer may ask why this needs a workflow at all. The CSV checks alone are ordinary functions. The project adds orchestration to demonstrate independent workers, visible failures, verified evidence, recovery, and an optional bounded model review. For one tiny file the orchestration adds overhead; its learning value is in making those behaviors explicit and inspectable.

## Honest portfolio wording

Before you have modified or deeply reviewed the project, an accurate description is:

> Developing an AI-assisted Python operations workflow project with CSV validation, snapshot comparison, parallel workers, persistent state, and bounded recovery. Studying the implementation and extending it through focused issues and tests.

After you have run, reviewed, and made a meaningful change, adapt this statement to your actual work:

> Built and extended an AI-assisted open-source Python workflow runner for CSV operations checks. Implemented [your specific contribution], verified [the behavior you tested], and documented retry, resume, and evidence-review tradeoffs.

Replace the bracketed details with real work. Claim the optional local model integration only after you test it. Claim a public repository only after you publish it. Avoid invented users, revenue, time savings, production scale, or professional employment history.

Keep your public engineering log short: problem, decision, implementation, validation, limitation. A few understandable commits and one well-explained improvement are stronger evidence than a long feature list you cannot defend.
