# Demo Guide

[README](../README.md) · [CLI reference](CLI_GUIDE.md) · [Validation](VALIDATION.md)

## Setup

Record from the repository root in Windows PowerShell with Python 3.12 installed. All data is synthetic. Use a readable terminal font beside the README/browser, hide unrelated windows, and keep the same PowerShell session so variables survive. No video editor or live model server is required. These commands create fresh state rather than deleting old runs.

## Two-to-three-minute recording

| Time | Screen | Narration |
| --- | --- | --- |
| 0:00–0:20 | README pitch and diagram | “This models recurring CSV review with durable recovery and human approval. The default checks are deterministic Python.” |
| 0:20–0:45 | Enqueue IDs, QUEUED, attempts 0; empty runs | “Receipt reserves identity without executing specialists.” |
| 0:45–1:05 | Worker result and waiting job | “One worker claims the job, checks the data, and saves a briefing.” |
| 1:05–1:35 | Rejection, revision completion, approval versions | “Feedback creates a new briefing version from saved evidence.” |
| 1:35–2:00 | Approve; queue SUCCEEDED and event COMPLETED | “Approval completes the same job and run.” |
| 2:00–2:30 | HTML report, findings and approval history | “The report is derived from SQLite and preserves the review trail.” |
| 2:30–2:50 | Actual test summary or Actions page | “Tests exercise races, crashes and installed-package behavior. Dead letters retain exhausted deliveries for replay.” |

### 1. Enqueue and inspect

```powershell
$state = ".opscheck/recording-" + [guid]::NewGuid().ToString("N")
$job = py -3.12 -m opscheck enqueue opscheck/examples/inbox/job-001.opscheck.json --state-dir $state | ConvertFrom-Json
$job | Select-Object id, ingestion_id, workflow_run_id, status, attempts
py -3.12 -m opscheck job $job.id --state-dir $state
py -3.12 -m opscheck runs --state-dir $state
```

Expected: `QUEUED`, attempts `0`, one job/event/reserved run ID. `runs` prints nothing because the reserved workflow has not executed. Job inspection includes availability, lease metadata and audit/attempt history.

### 2. Run one worker

```powershell
py -3.12 -m opscheck worker --once --worker-id recording-worker --state-dir $state
$waiting = py -3.12 -m opscheck job $job.id --state-dir $state | ConvertFrom-Json
$waiting | Select-Object id, status, workflow_run_id, attempts, lease_owner
py -3.12 -m opscheck run $job.workflow_run_id --state-dir $state
```

Expected: `WAITING_FOR_APPROVAL`, attempts `1`, released lease, original reserved run. The run output includes the briefing. Finding issues in the messy orders is expected and does not indicate a worker failure.

### 3. Reject, revise and review the new version

```powershell
py -3.12 -m opscheck reject $job.workflow_run_id --reviewer "Aerol" --comment "Explain changed records." --state-dir $state
py -3.12 -m opscheck approvals $job.workflow_run_id --state-dir $state
py -3.12 -m opscheck run $job.workflow_run_id --state-dir $state
```

Expected: revision completion, version 1 `REJECTED`, version 2 `PENDING`, run still `WAITING_FOR_APPROVAL`. The new `revision_agent_1` task adds clarification from saved evidence. Quality/change specialists keep their original attempt counts. A human revision does not create a new queue delivery.

### 4. Approve and inspect completion

```powershell
py -3.12 -m opscheck approve $job.workflow_run_id --reviewer "Aerol" --state-dir $state
py -3.12 -m opscheck queue --state-dir $state
py -3.12 -m opscheck event $job.ingestion_id --state-dir $state
py -3.12 -m opscheck run $job.workflow_run_id --state-dir $state
Invoke-Item "$state/$($job.workflow_run_id)/report.html"
```

Expected: queue `SUCCEEDED`, event `COMPLETED`, workflow `SUCCEEDED`, one delivery. In the HTML report show the workflow, queue metadata, original findings, and both approval versions. The reviewer name is local asserted metadata, not an authenticated account.

### 5. Show real validation evidence

Before recording, run the full suite and leave its final summary ready in another terminal:

```powershell
py -3.12 -m unittest discover -s tests -v
```

During the recording, show that actual summary or the [GitHub Actions workflow](https://github.com/MasterAerol/opscheck-flow/actions/workflows/ci.yml). State its displayed result accurately; do not label pending CI as passed. The [validation record](VALIDATION.md#portfolio-polish-validation) records this documentation pass. End by mentioning bounded retries and dead-letter replay; a separate longer clip can show the next example.

## Dead-letter and replay demo

Use a one-attempt delivery budget to keep this demonstration short. `--fail-job` is an explicit demo-only worker flag; it is not accepted in a manifest. Exit code `2` on the injected failure is expected.

```powershell
$deadState = ".opscheck/deadletter-demo-" + [guid]::NewGuid().ToString("N")
$deadJob = py -3.12 -m opscheck enqueue opscheck/examples/inbox/job-001.opscheck.json --max-attempts 1 --state-dir $deadState | ConvertFrom-Json
py -3.12 -m opscheck worker --once --worker-id failure-demo --fail-job --state-dir $deadState
py -3.12 -m opscheck deadletters --state-dir $deadState
py -3.12 -m opscheck job $deadJob.id --state-dir $deadState

py -3.12 -m opscheck replay $deadJob.id --state-dir $deadState
# Omit the failure flag; replay has a fresh delivery budget.
py -3.12 -m opscheck worker --once --worker-id replay-demo --state-dir $deadState
py -3.12 -m opscheck approve $deadJob.workflow_run_id --reviewer "Aerol" --state-dir $deadState
py -3.12 -m opscheck job $deadJob.id --state-dir $deadState
py -3.12 -m opscheck event $deadJob.ingestion_id --state-dir $deadState
```

Expected sequence: `QUEUED → DEAD_LETTER → QUEUED → WAITING_FOR_APPROVAL → SUCCEEDED`. Job/event/run IDs remain unchanged, replay count becomes `1`, and both delivery attempts remain visible. The event finishes `COMPLETED`. Backoff with a larger budget is covered by the deterministic queue tests and installed smoke.

## Authentic visuals to capture later

Capture the generated HTML report with its real approval history, or a short terminal recording showing the same IDs from enqueue through approval. Use only synthetic data and inspect visible paths for private information. No screenshot, GIF, or video is supplied or fabricated by this documentation pass; the README is complete without one. The existing [Milestone 1 report](demo/report.html) remains a historical artifact, not a current queue demo.
