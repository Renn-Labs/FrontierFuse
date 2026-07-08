# Security Policy

FableFuse is local-first tooling that shells out to AI coding CLIs. Please help keep it safe.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the maintainers (open a GitHub security advisory, or
contact a maintainer directly) — **do not** open a public issue for an undisclosed vulnerability.
Include repro steps and impact. We aim to acknowledge within a few days.

## Scope & things to know

- **The hard gate is a workflow guardrail, not a sandbox.** It steers an in-session brain away from
  direct execution; it does not sandbox the selected body/lead engine. The Codex body runs with
  `--yolo` by default (it may run commands/tests), and Claude-CLI bodies have their own tool
  permissions. Only point any lead/body executor at repositories you trust, and review diffs.
- **Kill-switch:** `FABLE_GUARDS_OFF=1` (or `CLAUDE_GUARDS_OFF=1`) disables both hooks.
- **No secrets in the repo.** FableFuse reads no API keys itself; the underlying `codex` / `claude`
  CLIs manage their own auth. Never commit keys, provider logs, generated `runs/`, or `verdict.json`.
- **Untrusted output.** Treat all model/body output as untrusted data — verify it with the
  deterministic gate and your own review before relying on it.

## Supported versions

FableFuse is pre-1.0; only the latest `main` is supported.
