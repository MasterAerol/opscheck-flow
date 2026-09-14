# Security policy

OpsCheck Flow is a local v0.1 Python tool. It has no hosted account system. This is an early project without a formal security audit or guaranteed response window.

## Supported code

Security fixes target the current `main` branch. There is no promised maintenance window for older snapshots. This is a local-first demonstration project, not formally audited security software.

## Report a vulnerability

If this GitHub repository has private vulnerability reporting enabled, use its **Security → Advisories → Report a vulnerability** option. If that option is unavailable, open a minimal public issue asking the maintainer to establish a private reporting channel. Do not include an exploit, credentials, private exports, or sensitive source values in that public request. This project does not currently publish a dedicated security email address.

Include the affected version, a minimal synthetic reproduction, the expected trust boundary, the observed behavior, and the potential impact through the available private channel. Ordinary correctness bugs can use the public bug template.

## Data and execution boundaries

- Default execution reads local CSV/JSON inputs and writes local reports and SQLite run state. It does not need a network connection or a paid API.
- Optional model mode sends a bounded sample of findings to the configured loopback model server. Inspect that server's own behavior and configuration before using private data; OpsCheck cannot guarantee what an independently installed server does with requests.
- The model adapter restricts URLs to loopback chat-completions endpoints, disables redirects and environment proxies, and uses request/response size limits and timeouts.
- Model outputs are untrusted. Schema checks and evidence-ID checks constrain structure, but they do not prove every claim or recommendation. Neither analyst nor reviewer can run tools, edit exports, or authorize real-world changes.
- CSV cells and model text should remain inert in generated HTML. Report generation must escape untrusted values rather than inserting them as markup.
- `.opscheck/`, JSON/HTML reports, and database files may contain personal or commercially sensitive values. They are not an encrypted vault. Use the operating system's file permissions and disk protection as appropriate.
- Strict JSON manifests cannot select code or dynamic imports. Manifest paths are contained beneath their directory; state/report writes protect inputs and implemented symlink/hardlink aliases. SQLite constraints, local OS locks, and expiring lease tokens coordinate workers without authenticating them.
- Inputs have documented size/record limits. Processing remains in memory; do not treat these limits as a sandbox against hostile local users.

Use synthetic examples in issues and pull requests. Never commit real customer files or access tokens. The bundled fixtures use invented orders and reserved example email addresses.
