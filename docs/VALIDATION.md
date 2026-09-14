# Milestone 3 validation record

Validated on **15 September 2026 (Asia/Singapore)** with **Python 3.12.10 on Windows**. Branch: `feature/event-ingestion-idempotency`.

The branch initially pointed to Milestone 1. Before implementation it was rebased onto the locally available Milestone 2 commit `1c60be4`, preserving the existing human approval implementation. Main and remote branches were not changed.

## Automated results

Executed: `py -3.12 -m unittest discover -s tests -v`

| Result | Count |
| --- | ---: |
| Total | **174** |
| Passed | **172** |
| Failed/errors | **0** |
| Skipped | **2** |

The finalization run completed in 39.539 seconds. Both skips are POSIX symlink tests whose creation may require Windows privileges: the existing SQLite-sidecar protection test and the new source-symlink containment test. Windows hardlink protection, process locks, explicit SQLite closure, and database deletion were exercised successfully.

All 136 Milestone 1/2 tests remain unchanged and pass apart from the existing platform skip. The 38 new tests cover strict manifests, invalid diagnostics, relative path containment, renamed/copied manifests and data, explicit-ID aliases and conflicts, changed keys/rules/source bytes, deterministic one-shot scanning, real-process duplicate ingestion, crash recovery, event/run linkage, approval/revision continuity, terminal revision limits, retry, SQLite constraints and closure, output escaping, source drift, and report-write failure reporting.

Two Python processes were synchronized through stdin before ingesting the same manifest. Both commands completed cleanly; SQLite contained one canonical event, one reserved workflow ID, one claim, one workflow, and a duplicate count of one. No timing-based sleep determines the expected winner.

Crash tests terminated real Python processes after receipt persisted, after the claim/reservation transaction, after workflow insertion, during a worker, and after the workflow reached its human checkpoint before publication. Re-ingestion recovered one canonical event and one workflow in every case. Claim and reservation are a single transaction; both named crash scenarios therefore preserve the same committed bridge. A separate rollback test confirmed that neither claim nor reservation survives failure before commit.

Retry tests preserved completed specialist attempts and approval history, including recovery of a failed human revision after source files were removed. Changed inputs between receipt and execution failed without replacing the expected fingerprint; restoring the original bytes allowed retry under the same run ID. Status synchronization acquires a SQLite write transaction before reading authoritative run state. Report publication failure returns an error while preserving the committed waiting workflow and its audit evidence.

## Manual CLI lifecycle

Each command ran in a new Python process. The demo used `opscheck/examples/inbox/job-001.opscheck.json` and state directory `.opscheck/milestone3-demo`.

- Event: `evt_3eb4df6975774d52b6c0555486fbb8d1`
- Run: `9e2b35ef3f5841feae5d35a458090196`

| Check | Observed result |
| --- | --- |
| First ingest | Exactly one event and one run; WAITING_FOR_APPROVAL |
| Re-ingest same manifest | Duplicate; same event and run |
| Ingest renamed manifest with copied data directory | Duplicate; still one event and one run |
| Reject with “Explain changed records.” | Same run revised; approval #2 pending |
| Approve revised briefing | Workflow SUCCEEDED; event COMPLETED |
| Specialist attempts | Quality 1; change 1; unchanged through replay and human decisions |
| Explicit external-ID replay | Same event/run |
| Same external ID with changed key | INVALID conflict; no second workflow |

A separate fresh scan store processed an inbox containing two equivalent manifests:

| Scan | Scanned | New events | Duplicates | Workflows created |
| --- | ---: | ---: | ---: | ---: |
| First | 2 | 1 | 1 | 1 |
| Second | 2 | 0 | 2 | 0 |

The automated mixed-inbox test additionally processed two distinct jobs, a duplicate, and an invalid job, preserving deterministic counts and continuing past the invalid manifest.

## Packaging and installed smoke

An isolated PEP 517 build produced both `opscheck-0.1.0.tar.gz` and `opscheck-0.1.0-py3-none-any.whl`; the wheel was built from the source distribution. The nested demo manifest and its synthetic data were included as package resources.

Installed the final wheel with `pip install --no-deps` into a fresh Python 3.12 virtual environment, then executed `scripts/smoke_installed.py`. Every child command ran in a temporary directory outside the checkout with `PYTHONPATH` removed. The verified module path was the new environment's `Lib/site-packages/opscheck/__init__.py`.

Both smoke lifecycles passed:

1. Existing failure injection → retry → human pause → rejection/revision → approval, with escaped HTML and retained worker attempts.
2. Installed resource inbox → ingest → WAITING_FOR_APPROVAL → duplicate/same event and run → approve → event COMPLETED → scan with zero workflows created.

The existing CI workflow already invokes this smoke script for its Ubuntu Python 3.10/3.12/3.14 and Windows Python 3.12 matrix; the script was extended without changing that matrix. No remote GitHub Actions result is claimed.

`git diff --check` passed. Generated databases, locks, reports, test logs, virtual environments, distributions, package metadata, and caches are ignored. No runtime artifacts are included in the source changes.

## Finalization verification

The full suite, isolated package build, and fresh installed-wheel smoke were rerun successfully before committing. No implementation fixes were needed. The installed commands executed outside the checkout with `PYTHONPATH` removed and imported the fresh virtual environment's `site-packages` copy.

A new, previously nonexistent state directory was used at `.opscheck/finalization-8b79d15dbea24270b69e7e9a4fbb85b5/state`; no existing state was deleted. Separate CLI processes produced these results:

- Canonical event: `evt_8970327e81e542f7bb8c76b6aba5ab46`
- Canonical run: `67895cc238f04f938f4df6afce6baa6f`
- First ingest: one event, one run, `WAITING_FOR_APPROVAL`.
- Duplicate ingest: same event and run; still exactly one workflow.
- Scan #1 and scan #2: each found one duplicate and created zero workflows.
- Approval by `Aerol`: run `SUCCEEDED`, event `COMPLETED`.
- Specialist audit records were identical before and after approval; quality and change each retained one attempt.

After fetching origin, `origin/main` remained at Milestone 1 (`8a29773`). This branch contains Milestone 2 (`1c60be4`) and includes current main in its ancestry; a pull request into main therefore also includes the unmerged Milestone 2 changes. GitHub CLI was unavailable locally. No remote Actions result or merge is claimed.

## Remaining verification limits

- Only Windows Python 3.12.10 was available locally. Ubuntu and Python 3.10/3.14 were not independently executed for this milestone.
- Two symlink tests were skipped on Windows; corresponding POSIX behavior remains for CI verification.
- A live model server was not tested. Existing fake-server/model-adapter tests passed; event manifests deliberately configure deterministic local workflows only.
- Watch mode is not implemented. Scanning is a one-shot operation over top-level `*.opscheck.json` entries.
- Coordination is within one shared local SQLite store and OS lock domain. It is not distributed exactly-once processing, a cross-machine lease system, or an authenticated event service.
- Builds succeeded with the pre-existing setuptools deprecation warning for the legacy `project.license` table.

---

## Historical validation records

The records below describe previous milestones and are retained for context.

# Milestone 2 validation record

Validated on **15 September 2026 (Asia/Singapore)** with **Python 3.12.10 on Windows**. Branch: `feature/human-approval-loop`.

## Automated results

Command: `py -3.12 -m unittest discover -s tests -v`

- Total: **136**
- Passed: **135**
- Failed/errors: **0**
- Skipped: **1** — the existing POSIX symlink/SQLite-sidecar test requires symlink privileges on Windows.

The original 109 test cases remain. Run-level expectations were updated to the explicit uppercase statuses and human approval pause; worker assertions, retry budgets, parallel barriers, verifier checks, model transport tests, and SQLite cleanup checks remain. The completed-run resume test now approves the briefing before asserting completion. CLI test subprocesses explicitly use UTF-8 to remove a baseline Windows output-decoding thread exception.

The 27 new tests cover persisted pauses, decision metadata/timestamps, three-version histories, deterministic revision evidence, rejection limits, invalid operations, old approval IDs, SQLite uniqueness and immutability, HTML escaping, process restarts, competing processes, transaction rollback, interrupted revisions, report-write failures, evidence after source removal, source alias protection, connection closure, and migration from the original database schema.

Real process race checks exercised approve/approve (without an explicit approval ID), reject/reject, and approve/reject against the same version. Exactly one decision succeeded in each race. Crash tests terminated Python after a decision committed, during a revision, and after revision output committed but before the next approval request. Resume completed the saved transition without rerunning specialists or duplicating approval history.

## Manual human approval lifecycle

Each CLI invocation ran in a new Python process. Actual run:

`1d95ea0c19a64f058255db2956ae6f8a`

State directory: `.opscheck/milestone2-demo`

| Operation | Observed result |
| --- | --- |
| `flow-demo --fail-once quality_agent` | Normal exit; `WAITING_FOR_APPROVAL`; approval #1 pending |
| `run RUN_ID` | Saved waiting status, iteration 1, active approval ID, report location |
| `reject RUN_ID --reviewer Aerol --comment "Please make the change summary clearer."` | Revision agent completed; `WAITING_FOR_APPROVAL`; iteration 2 |
| `approvals RUN_ID` | #1 rejected with reviewer/comment/timestamp; #2 pending |
| `approve RUN_ID --reviewer Aerol` | Normal exit; `SUCCEEDED` |
| Final SQLite inspection | #1 rejected and #2 approved; no pending approval |

Quality worker lifetime attempts: **2**, including the injected first-attempt failure. Change worker lifetime attempts: **1**. Event inspection confirmed **neither specialist started after the initial approval request**. Both briefing snapshots and the full decision history remained queryable. The final HTML contained Approval History.

Malicious comments containing `<script>alert("x")</script>` were separately exercised by automated tests and the installed-package smoke test. The HTML contained escaped text and no script element.

## Build and installed package

Executed an isolated PEP 517 build with `python -m build --outdir dist`. Both `opscheck-0.1.0.tar.gz` and `opscheck-0.1.0-py3-none-any.whl` built successfully; the wheel was built from the source distribution.

Installed the wheel with `pip install --no-deps` into a fresh Python 3.12 virtual environment. Executed `scripts/smoke_installed.py`, which launches every command from a temporary directory outside the source checkout with `PYTHONPATH` removed. The imported module path was the environment's `Lib/site-packages/opscheck/__init__.py`. Version, failure injection, waiting inspection, rejection/revision, history, final approval, retained worker attempts, and escaped HTML all passed. The same smoke script is now included in the existing Linux/Windows GitHub Actions matrix.

`git diff --check` passed. `.opscheck/`, `dist/`, package metadata, and Python caches were confirmed ignored; no generated runtime artifacts appear as untracked source changes. No commit, push, merge, or remote publication was performed.

## Verification limits

- Linux and the other Python versions were not independently run for this milestone in this Windows environment. The CI matrix remains configured but no remote Actions result is claimed.
- The Windows symlink test remains skipped. Hardlink protection, explicit SQLite closure, database deletion, and real process locks were exercised on Windows.
- A live model server was not used. Existing deterministic fake-server/model-adapter tests passed; human revisions deliberately use local deterministic logic.
- Build succeeded with the existing setuptools deprecation warning about the legacy `project.license` table. Updating packaging license metadata is outside this milestone.
- Reviewer names are local asserted metadata. Revision generation clarifies verified findings; it does not interpret arbitrary editorial instructions or modify source CSVs.

---

## Historical Milestone 1 record

The following is the prior validation record, retained for context. Its pending Windows verification statement describes that earlier validation only.

# v0.1.0 validation record

Validated on 14 September 2026 with Python 3.12.14 on Linux.

## Automated checks

`python -m unittest discover -s tests -q` completed with **109 tests passing** after the Windows cleanup correction below.

| Area | Tests | Coverage highlights |
| --- | ---: | --- |
| CSV engines | 35 | Strict rules, finite decimal bounds, malformed CSV, key ambiguity, exact changes, finding caps, concurrent large-field parsing |
| Local model adapter | 19 | Actual HTTP test server, separate analyst/reviewer messages, rejection and revision, invented evidence IDs, exhausted loops, endpoint and size limits |
| CLI | 11 | Real subprocess commands, exit codes, generated reports, input protection including hardlinks and quoted home paths, demo and resume |
| Reports | 20 | Escaping, complete counts, truncation disclosure, workflow status, retry timeline, evidence mapping, local/model distinction |
| Workflow | 24 | Parallel execution barrier, transient failures, attempt limits, independent successes, SQLite state, crash recovery, same-run locks, input fingerprints, failed model-review history, explicit connection closure |

The same-run concurrency test also passed 30 consecutive executions after the
SQLite sidecar race fix. An independent review checked 300 deterministic generated
validation/comparison cases against count invariants.

## Windows cleanup correction

A user run of the original bundle on Windows completed the demo but reported one
test cleanup error (`WinError 32`) and one expected platform-specific skip.
The affected test used SQLite's transaction context manager without explicitly
closing its extra database connection. Windows then refused to delete the still-open
temporary database during cleanup.

The corrected test uses `contextlib.closing` and verifies that the connection is
closed on every operating system. Database initialization also now closes its
connection if setup raises before ownership reaches the workflow runner. A new
regression exercises that failure using an actual corrupt SQLite file. The full
109-test suite passes on Linux; a corrected Windows run is still pending.

## Installation and demo

Built a wheel with setuptools and installed it without dependencies into a fresh
virtual environment. From a directory outside the source checkout, the installed
`opscheck --version`, `opscheck flow-demo --fail-once quality_agent`, and
`opscheck runs` commands all succeeded.

The included [workflow report](demo/report.html) comes from that installed demo.
The quality worker succeeded on attempt 2. The input contains 10 synthetic orders,
8 validation issues affecting 8 records, and snapshot changes of 1 added,
1 removed, 2 modified, and 7 unchanged orders. The saved JSON includes run events,
attempt totals, results, and the evidence-ID mapping.

## Practical limits of this validation

- No live language model was run. HTTP fixtures verify adapter behavior; they do
  not measure the accuracy or compatibility of a particular local model.
- HTML content, escaping, responsive CSS, and relative links were checked in
  tests. The preview browser blocked local HTML navigation, so rendered layout
  was not visually verified in this environment.
- The GitHub Actions configuration targets Linux Python 3.10/3.12/3.14 and
  Windows Python 3.12. Those hosted jobs have not run yet; publishing the
  repository and checking Actions is required to confirm them.
- This is an initial open-source portfolio release using synthetic fixtures.
  No production deployment, customer adoption, or business time savings are claimed.
