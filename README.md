# OpsCheck Flow

**A local workflow runner that checks CSV exports, compares changes, verifies its work, and produces an operations briefing.**

OpsCheck Flow is a small, inspectable Python project for learning workflow orchestration and agent systems through a concrete operations task. An operator receives a new orders export: are required fields missing, which records changed, and what should someone review first? OpsCheck runs the checks in parallel, verifies the evidence, records each task attempt, and resumes interrupted work from local state.

The default workflow runs with **Python 3.10+ and zero third-party runtime dependencies**. It requires no paid API, account, hosted service, or model download. An optional local model mode adds separate analyst and reviewer subagents with a bounded revision loop. The deterministic workers in the default demo are Python tasks, not LLM agents.

**Status:** v0.1.0, a local CLI and Python library. The project includes synthetic data, tests, contribution templates, and a CI configuration. It does not claim production users or a live hosted deployment.

**Included demo:** download or extract the project and open [docs/demo/report.html](docs/demo/report.html) in a browser to inspect a completed run with a recovered worker failure. The corrected source passes **109 automated tests** on Linux; see [the validation record](docs/VALIDATION.md) for the Windows cleanup correction and remaining verification limits.

## Try the complete workflow

From the project directory:

```bash
python -m opscheck flow-demo
```

On systems where Python 3 is named `python3`, substitute `python3` throughout. The command prints the run ID, task states, attempts, and report locations. Open the printed `report.html` path for the workflow dashboard. Local execution state is stored under `.opscheck/runs/` by default; each run has `report.json`, `report.html`, `quality.html`, and `changes.html` when its tool results are available.

To make recovery visible, inject one temporary failure:

```bash
python -m opscheck flow-demo --fail-once quality_agent
python -m opscheck runs
```

The quality worker fails on its first attempt, retries within the configured limit, and then succeeds. The independent comparison worker can complete while this happens. Inspect the run's ordered events and attempt counts to see the recovery.

Run `python -m opscheck flow-demo --resume RUN_ID` with a printed demo ID to see successful tasks reused from saved state.

The bundled example contains 10 synthetic orders. Its quality check finds 8 issues across 8 records. The comparison finds 1 added order, 1 removed order, 2 modified orders, and 7 unchanged orders. A successful workflow can contain data-quality findings: success means the pipeline completed and verified its results.

## What the workflow does

```mermaid
flowchart TD
    P[Planner] --> Q[Quality worker]
    P --> C[Change worker]
    Q --> V[Verifier]
    C --> V
    V --> B[Briefing]
    Q -->|Temporary failure| R[Bounded retry]
    R --> Q
```

| Role | Responsibility | Implementation |
| --- | --- | --- |
| Planner | Declare dependencies before execution | A fixed, explicit task plan |
| Quality worker | Apply a JSON rule set to a CSV export | Deterministic Python validation |
| Change worker | Compare two CSV snapshots by a stable key | Deterministic Python comparison |
| Verifier | Check result structure, counts, sources, and recomputed results | A separate deterministic verification step |
| Briefing | Summarize verified evidence | Deterministic by default; optional analyst/reviewer model team |

SQLite stores task states, attempts, outputs, and an event history. Successful tasks are reused on resume. Input contents and relevant configuration are fingerprinted so a resumed run cannot silently mix old and new inputs. The verifier reruns the same engines; it checks consistency and orchestration integrity, not correctness through an independently developed algorithm.

## Run it on your files

The following single-line command also works from a source checkout:

```bash
python -m opscheck flow opscheck/examples/orders-messy.csv --rules opscheck/examples/rules.json --before opscheck/examples/orders-before.csv --after opscheck/examples/orders-after.csv --key order_id --json reports/flow.json
```

To resume that run, repeat the same input and configuration options and add `--resume` with the run ID printed by the first command:

```bash
python -m opscheck flow opscheck/examples/orders-messy.csv --rules opscheck/examples/rules.json --before opscheck/examples/orders-before.csv --after opscheck/examples/orders-after.csv --key order_id --resume RUN_ID --json reports/flow.json
```

Replace `RUN_ID` with an existing ID; it is not a literal example run. Use `--state-dir PATH` consistently if you change the state location. `--max-attempts` accepts 1–5 and defaults to 2. This is an allowance per task for the current invocation; accumulated attempt counts remain visible across resumed invocations. Invalid CSV or rule configuration is not a temporary failure and is not retried automatically.

For a repeatable failed-run demonstration, run the first command with `--fail-once quality_agent --max-attempts 1`. Resume it with the same input/configuration options and `--resume RUN_ID`. The injected failure applies only to the task's first lifetime attempt.

## Optional local model subagents

If you already have a model running on a compatible local server, add its chat-completions endpoint and installed model name:

```bash
python -m opscheck flow opscheck/examples/orders-messy.csv --rules opscheck/examples/rules.json --before opscheck/examples/orders-before.csv --after opscheck/examples/orders-after.csv --key order_id --llm-url http://127.0.0.1:11434/v1/chat/completions --model LOCAL_MODEL --max-rounds 2
```

Replace `LOCAL_MODEL` with a model already available on your server. OpsCheck does not install models or provide the model server. Local model execution still needs suitable hardware, memory, and electricity. This option is unnecessary for the standard demo.

The analyst proposes actions with evidence IDs. A separate reviewer context accepts the briefing or requests changes. Each round makes up to one analyst call and one reviewer call, up to the chosen limit of 1–3 rounds; the default is 2. Invalid analyst output can stop a round before its reviewer call. A transient transport failure can restart the briefing task within the workflow attempt limit. Only loopback endpoints are accepted. Models receive a bounded evidence sample and cannot execute tools or modify input files.

Schema checks reject malformed answers and invented evidence IDs. Reviewer approval is a model judgment and can be wrong; it is not a factual guarantee. The adapter has automated fake-server tests. A real local model server was not exercised for this initial build, so compatibility and output quality still need validation with your chosen server and model.

## Use the underlying tools separately

Generate both standalone demo reports:

```bash
python -m opscheck demo
```

Open `reports/demo/validation.html` and `reports/demo/comparison.html` in your browser. Corresponding JSON files are created alongside them. All demo names, emails, and transactions are synthetic.

Validate an export:

```bash
python -m opscheck validate opscheck/examples/orders-messy.csv --rules opscheck/examples/rules.json --json reports/validation.json --html reports/validation.html
python -m opscheck validate opscheck/examples/orders-valid.csv --rules opscheck/examples/rules.json
```

Compare snapshots:

```bash
python -m opscheck compare opscheck/examples/orders-before.csv opscheck/examples/orders-after.csv --key order_id --json reports/comparison.json --html reports/comparison.html
```

Use `--delimiter ";"` for semicolon-delimited files. A delimiter must be one character. Run `python -m opscheck --help` or append `--help` to a command for its options.

### Rules and exact matching behavior

```json
{
  "version": 1,
  "columns": {
    "order_id": {"required": true, "unique": true},
    "email": {"type": "email"},
    "total": {"type": "number", "min": 0},
    "status": {"allowed": ["paid", "pending", "refunded"]},
    "order_date": {"type": "date"}
  }
}
```

| Rule | Meaning |
| --- | --- |
| `required` | Blank or whitespace-only cells fail |
| `unique` | Every nonblank occurrence after the first duplicate fails |
| `type` | One of `string`, `integer`, `number`, `email`, or `date` |
| `min`, `max` | Inclusive finite numeric bounds; require `number` or `integer` |
| `allowed` | A nonempty list of accepted strings, matched case-sensitively |

Unknown configuration keys or rule types are errors. A missing configured column produces a finding even if its cells are optional. Optional blank cells skip type, bound, allowed-value, and uniqueness checks. Validation trims outer cell whitespace without changing the input. Dates must be real calendar dates in `YYYY-MM-DD` format; numbers use finite `Decimal` values. Email checking checks basic syntax, not whether an inbox exists.

Headers and keys are case-sensitive. Comparison trims only the matching key; shared non-key values are compared exactly, so `"10"` and `"10.0"`, or `"paid"` and `"paid "`, are changes. Blank or duplicate comparison keys are errors. Added and removed columns are reported separately from changed records. A schema-only change gives status `changed` even when `total_changes` is zero. Record findings are sorted by key for reproducibility.

### Exit codes

| Command | `0` | `1` | `2` |
| --- | --- | --- | --- |
| `validate` | Data passes rules | Validation findings | Invalid input, rules, or I/O |
| `compare` | No record or schema changes | Changes found | Invalid input or I/O |
| `demo` | Reports generated successfully | — | Demo or I/O error |
| `flow`, `flow-demo` | All workflow steps completed and verified | — | Workflow failed, invalid configuration, or I/O error |
| `runs` | Run listing succeeded | — | State or I/O error |

Exit `1` from the intentionally messy standalone validation is expected. Workflow commands return `0` after successfully identifying those same business problems; inspect the embedded quality and comparison results before making an operational decision.

## Installation and checks

Source checkout execution needs no installation. To install the package in your active Python environment:

```bash
python -m pip install .
python -m opscheck --version
```

Packaging may obtain build tools through pip. The installed runtime itself has no third-party package dependencies. To run the repository's tests:

```bash
python -m unittest discover -s tests -v
```

## Scope and limits

- CSV input is UTF-8, with an optional BOM, and requires a header. Header-only files are valid. Blank or duplicate headers, malformed records, and row-width mismatches are errors.
- Each input CSV is limited to 10 MiB and 100,000 data records. Processing is in memory; this is a tool for modest exports.
- Source row numbers count logical CSV records, starting at 2 after the header. A quoted field containing newlines still belongs to one record.
- Tool reports contain at most the first 500 findings by default. Aggregate counts include omitted findings. Standalone `validate` and `compare` accept `--max-findings` from 1–5000; the Python API also exposes `max_findings`.
- Model evidence is further sampled and size-limited; the evidence payload reports truncation. A briefing is not a complete replacement for the tool reports.
- Report directories are created as needed, and existing report files at the specified destinations are overwritten. Input and rule-file paths are protected from report overwrite.
- Local run state and reports can contain source values. Keep real exports, `.opscheck/`, and generated private reports out of commits.
- v0.1 has no web service, account system, scheduler, Excel parser, automatic source-data repair, or autonomous business actions. The planner uses a fixed DAG; it does not ask a model to invent a workflow.

## Learn, contribute, and publish

Read [the architecture](docs/ARCHITECTURE.md) for the execution model, [the learning guide](docs/LEARNING.md) for a practical study path and interview demo, and [CONTRIBUTING.md](CONTRIBUTING.md) for changes. Security reporting is described in [SECURITY.md](SECURITY.md).

To publish this project to your own GitHub account, use an account and repository name you control. Review the files before making them public; the bundled examples are safe synthetic fixtures. From the project directory, initialize Git only if it is not already initialized:

```bash
git init -b main
git add .
git status --short
git commit -m "Initial OpsCheck Flow release"
gh auth login
gh repo create opscheck-flow --public --source=. --remote=origin --push
```

If the source is already committed, skip the initialization/commit steps. If the account already has `opscheck-flow`, choose an available repository name. If a remote named `origin` already exists, review `git remote -v` and use the existing remote or another appropriate remote name. The final command creates a public repository and pushes the current branch. See the [official GitHub CLI instructions](https://cli.github.com/manual/gh_repo_create).

Without the GitHub CLI, create a public repository in GitHub's browser interface, then use **Add file → Upload files** to upload the extracted project contents while preserving folders. Upload source files rather than only a ZIP so GitHub can show the code and run CI. Include `.github/` and `.gitignore`; exclude `.git/`, local run state, private reports, and virtual environments. If your phone's file picker cannot preserve directories, upload within matching repository folders or use a computer for the initial upload. Follow [GitHub's browser-upload guide](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository). Check the Actions tab after publishing; a committed CI configuration alone does not establish a passing GitHub run.

Suggested repository description: **Local Python workflow runner with parallel data checks, verification, persistent recovery, and optional analyst/reviewer subagents.**

## License

[MIT](LICENSE). Contributions are welcome.
