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
