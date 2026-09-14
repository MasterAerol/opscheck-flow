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
