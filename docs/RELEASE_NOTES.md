# Proposed Release Notes

**Draft only — no tag or GitHub release is created by this document.**

Recommended first formal tag: **v0.1.0**. Suggested title: **OpsCheck Flow v0.1.0 — Durable local review workflows**.

The package metadata, Python module and CLI currently agree on `0.1.0`, with Alpha development status. Keeping that pre-1.0 version avoids a cosmetic bump for documentation. Four milestones do not imply four published minor versions or a stable public API. Before tagging, verify remote tags/releases, merge the reviewed polish, rerun release checks, and confirm package/API/schema compatibility. If maintainers choose a new version, update both package and module metadata together before building.

## Summary

OpsCheck Flow models operational CSV review using Python and SQLite: parallel deterministic checks, verified evidence, durable human approval, idempotent events, and a local worker queue that recovers interrupted deliveries.

## Highlights

- Run the bundled synthetic demo without an account, paid API, model download or third-party runtime dependencies.
- Validate quality and compare snapshots concurrently, persist task outputs, and resume incomplete work.
- Reject and revise a saved briefing, then approve its new version with preserved decision history.
- Ingest strict manifests or enqueue them for later processing; duplicate content reuses canonical identity.
- Coordinate local workers with atomic claims, renewable leases, token fencing and OS execution locks.
- Retain exhausted deliveries as dead letters and replay them without replacing their event or run.
- Optionally generate a briefing through a bounded local analyst/reviewer model loop.

## Reliability corrections

SQLite connections explicitly close, including error and heartbeat paths. Inspection cannot revoke an unexpired worker lease; expired successful checkpoints reconcile without a false delivery failure. Both areas have regression coverage.

## Validation and compatibility

Use the [latest local validation](VALIDATION.md#portfolio-polish-validation) and inspect [GitHub Actions](https://github.com/MasterAerol/opscheck-flow/actions/workflows/ci.yml) for the exact release commit before publication. The matrix targets Ubuntu Python 3.10/3.12/3.14 and Windows Python 3.12. Local release checks build an sdist/wheel and run installed smoke outside the checkout. No live-model compatibility or quality is established by fake-server tests.

Requires Python 3.10+. The runtime has no third-party dependencies. Public interfaces and the state schema remain early and may evolve; this draft makes no general future migration guarantee.

## Scope

Coordination is local to a shared SQLite store and OS filesystem locks. There is no distributed consensus, independent-machine lease guarantee, network queue server, hosted service or authenticated reviewer identity. Incomplete computation can repeat after a crash. Reports and state may contain input values and are not encrypted by the application. This is a demonstration project without a formal security audit or production-customer reliability claim.

## Try it

Follow the [quick start](../README.md#quick-start) or [recording walkthrough](DEMO_GUIDE.md). Read the [changelog](../CHANGELOG.md) for grouped changes and [security policy](../SECURITY.md) for reporting guidance.
