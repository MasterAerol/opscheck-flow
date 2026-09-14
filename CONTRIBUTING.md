# Contributing to OpsCheck Flow

Small, explainable improvements are welcome: fix a reproducible bug, improve a rule's error message, document an edge case, or add a focused capability with tests.

## Set up

Use Python 3.10 or newer. From a source checkout:

```bash
python -m opscheck flow-demo
python -m unittest discover -s tests -v
```

No third-party runtime package is required. Installing with `python -m pip install .` is optional for source development. Test new behavior from the checkout so an older installed copy cannot mask it.

## Make a change

1. Describe the problem and expected behavior in an issue or your pull request. For a large feature, discuss scope before implementation.
2. Create a short-lived branch and keep the change focused.
3. Add a synthetic fixture or regression test when behavior changes. Do not add actual customer exports, addresses, credentials, or private run histories.
4. Update the relevant README or architecture section if public behavior changes.
5. Run the test suite and the relevant CLI command. Report what you actually ran; do not describe an unrun check as passing.
6. Open a pull request that explains the trigger, the new behavior, and validation evidence.

## Engineering expectations

- Keep the core engines deterministic and separate from the CLI, orchestration, reports, and optional model adapter.
- Prefer standard-library solutions unless a dependency provides a clear benefit that the maintainers agree to accept.
- Treat input values as data. Never evaluate rule strings as code or let model text execute commands.
- Preserve documented exit codes, strict config validation, stable finding order, and aggregate counts when reports are capped.
- Bound every retry and model revision loop. Distinguish transient failures from invalid configuration.
- Preserve source files and avoid writing reports over inputs, including path aliases.
- Test concurrency with synchronization primitives such as barriers; do not rely on short sleep timing or machine-speed assumptions.
- Keep examples small enough to inspect manually. A future feature is a proposal until implemented and verified.

If your change alters the JSON output shape, rule semantics, resume fingerprint, or state database, explain its compatibility impact. A migration or version increment may be needed.

## AI-assisted contributions

AI tools are welcome. You remain responsible for reviewing the diff, understanding the code, running appropriate checks, and respecting licenses. Explain your own contribution accurately. Generated code alone is not evidence of production experience; an understood, tested improvement is useful evidence of engineering work.

## Reporting problems

Use the bug template with a minimal synthetic CSV/rules example, the command, Python version, expected behavior, and actual result. Redact file paths or source values when needed. Use [SECURITY.md](SECURITY.md) for security-sensitive reports.

Contributions are provided under the project's [MIT license](LICENSE).
