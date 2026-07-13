# Security Policy

FrontierFuse is local-first tooling that shells out to AI coding CLIs. Please help keep it safe.

## Reporting a vulnerability

Report suspected vulnerabilities privately to the maintainers (open a GitHub security advisory, or
contact a maintainer directly) — **do not** open a public issue for an undisclosed vulnerability.
Include repro steps and impact. We aim to acknowledge within a few days.

## Scope & things to know

### Workflow guardrail, not isolation

FrontierFuse's host hooks are a **workflow guardrail** for Claude Code orchestrator sessions. They
steer the in-session controller away from direct mutation and require a snapshot-bound GREEN before
a clean stop. They are **not** a sandbox, container, or security isolation boundary.

Anyone who owns the host (or can set env vars / edit settings / run tools outside the hooked
surface) can:

- set `FRONTIER_GUARDS_OFF=1` or `CLAUDE_GUARDS_OFF=1` (kill-switch; disables both hooks)
- `frontier-dispatch disarm` from a host shell that is not subject to the PreToolUse policy
- alter or remove hooks, session state, or config
- run body CLIs or editors outside Claude Code's hooked tool surface

Never treat an armed session as process isolation, multi-tenant security, or a substitute for OS /
network controls.

### What the armed Claude hook surface does

While a session is **armed** on Claude Code's PreToolUse hook:

- file-mutation tools (`Write` / `Edit` / `MultiEdit` / `NotebookEdit`) are denied
- non-allowlisted Bash is denied (argv-validated; shell chaining rejected)
- direct body CLI invocation (`codex` / `claude` / `grok` / `gemini` and common wrappers) is denied
- read-only inspection and the required `frontier-dispatch` loop commands remain; `config` is
  read-only while armed

The Stop hook blocks a clean finish unless a **fresh snapshot-bound GREEN** exists (zero gate exit,
stable matching workspace snapshot, not legacy, not unsafe shell). Prose "GREEN" never closes the
loop. Stop validation does not disarm or consume GREEN because other host hooks may still block the
same Stop event. It fences queued work until either the host exits or a subsequent PreTool event
reopens the session and invalidates GREEN; only explicit `frontier-dispatch done` performs the
consuming close transition.

### Frozen verifier (host-approved)

The host freezes the exact acceptance command at arm time:

```bash
frontier-dispatch arm --gate "<single argv command>" [--cwd PATH]
```

While armed, the controller calls `frontier-dispatch verify` without `--gate` or `--cwd`; any
restatement or replacement is refused. Default gate execution uses `shell=False` (argv). The
`--legacy-shell` / `FRONTIER_VERIFY_LEGACY_SHELL=1` path is explicit **unsafe compatibility** and
**cannot** satisfy the hardened close.

A closable arm requires a Git worktree. The receipt is bound to the arm-time argv and cwd, and
the snapshot covers Git-visible state plus bounded non-ignored untracked files. Git-ignored build
and cache paths are intentionally excluded so normal tools do not make every verdict stale.

### Body permissions (opt-in autonomy)

By default, body CLIs inherit **provider defaults** - FrontierFuse does **not** add
autonomous elevated flags unless the host opts in:

| Env | Effect when set |
|-|-|
| `FRONTIER_CODEX_YOLO=1` | adds Codex `--yolo` |
| `FRONTIER_GROK_YOLO=1` | adds Grok `--permission-mode bypassPermissions` |
| `FRONTIER_GROK_PERMISSION_MODE=<mode>` | explicit Grok permission mode |

Only point any lead/body executor at repositories you trust, and review diffs.

### Data leaving the machine

Cross-provider prompts and context leave the local machine when a body/advisor CLI calls a remote
provider. **Provider terms, logging, and retention apply.** FrontierFuse does not re-implement
provider privacy policy.

Local state and artifacts (session state, config, `runs/`, `verdict.json`, managed prompt files)
are written **owner-only** (`0600` files / `0700` dirs where applicable). Treat body transcripts
and gate stdout tails as sensitive: they can contain secrets from the workspace or provider
output. Never commit `runs/`, `verdict.json`, provider logs, credentials, or private absolute
paths.

### Untrusted output

Treat all model/body output as untrusted data — verify with the deterministic gate and your own
review before relying on it.

## Supported versions

FrontierFuse is pre-1.0; only the latest `master` release is supported.
